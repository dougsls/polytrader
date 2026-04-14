"""PolyTrader — entry point.

Orquestra scanner + tracker + executor + notifier com graceful shutdown.
Inicializa schema (idempotente), configura logging, e entra em asyncio.gather.
"""
from __future__ import annotations

import asyncio
import signal
import sys
from typing import Any

import aiosqlite

from src.api.auth import prefetch_credentials
from src.api.balance import USDCBalanceFetcher
from src.api.clob_client import CLOBClient
from src.api.data_client import DataAPIClient
from src.api.gamma_client import GammaAPIClient
from src.api.http import close_http_client
from src.api.startup_checks import check_geoblock, latency_baseline
from src.api.websocket_client import RTDSClient
from src.core.config import get_settings
from src.core.database import DEFAULT_DB_PATH, init_database
from src.core.logger import configure_logging, get_logger
from src.core.models import CopyTrade, TradeSignal
from src.core.state import InMemoryState
from src.executor.balance_cache import BalanceCache
from src.executor.copy_engine import CopyEngine
from src.executor.position_sync import reconcile_bot_positions
from src.executor.risk_manager import RiskManager
from src.executor.risk_state import build_risk_state
from src.notifier.telegram import TelegramNotifier
from src.scanner.scanner import Scanner
from src.scanner.wallet_pool import WalletPool
from src.tracker.trade_monitor import TradeMonitor

log = get_logger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 60


async def heartbeat_loop(shutdown: asyncio.Event) -> None:
    while not shutdown.is_set():
        log.info("heartbeat")
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def amain() -> None:
    settings = get_settings()
    configure_logging(settings.env.log_level)
    log.info(
        "startup",
        mode=settings.config.executor.mode,
        vps=f"{settings.env.vps_provider}-{settings.env.vps_location}",
    )

    await init_database()

    # --- shared SQLite connection (Phase 0 — preserva ganho iter 6) ------
    shared_conn: aiosqlite.Connection = await aiosqlite.connect(DEFAULT_DB_PATH)
    log.info("shared_conn_opened")

    # --- notifier (cedo pra poder alertar em falhas de startup) ----------
    notifier = TelegramNotifier(
        token=settings.env.telegram_bot_token,
        chat_id=settings.env.telegram_chat_id,
    )

    # --- Phase 5: startup checks ----------------------------------------
    geo = await check_geoblock()
    if geo.get("blocked"):
        notifier.notify(
            f"🚨 IP BLOQUEADO pela Polymarket ({geo.get('country')}). "
            "Trading indisponível no exchange internacional."
        )
        if settings.env.exchange_mode == "international":
            log.critical("geoblock_fail_fast", **geo)
            await shared_conn.close()
            await close_http_client()
            sys.exit(1)

    rtts = await latency_baseline()
    clob_rtt = rtts.get("clob")
    if clob_rtt and clob_rtt > settings.env.latency_alert_threshold_ms:
        notifier.notify(
            f"⚠️ Latência alta no startup: CLOB={clob_rtt:.0f}ms "
            f"(threshold {settings.env.latency_alert_threshold_ms}ms)"
        )

    # --- clients (criados antes do state pois o sync precisa do data_client) -
    data_client = DataAPIClient()
    gamma = GammaAPIClient()

    # --- in-memory state cache (Phase 4) ---------------------------------
    state = InMemoryState()
    await state.reload_from_db(conn=shared_conn)

    # --- Orphan recovery ANTES de abrir WS (live-trading final check) ----
    # Se a VPS reiniciou no meio de um fill, o DB local pode estar defasado
    # em relação à posição on-chain. Pull /positions e reconcilia ANTES de
    # o tracker começar a emitir sinais de SELL (que consultam state RAM).
    if settings.env.funder_address:
        try:
            stats = await reconcile_bot_positions(
                data_client=data_client,
                wallet_address=settings.env.funder_address,
                conn=shared_conn,
                state=state,
            )
            if stats["added"] or stats["updated"] or stats["closed"]:
                notifier.notify(
                    f"🔄 Orphan sync: +{stats['added']} / ~{stats['updated']} "
                    f"/ ✕{stats['closed']}"
                )
        except Exception as exc:  # noqa: BLE001 — não bloquear startup
            log.error("position_sync_failed", err=repr(exc))
            notifier.notify(f"⚠️ Position sync falhou no startup: {exc!r}")
    else:
        log.warning("position_sync_skipped", reason="funder_address_empty")

    # Phase 5 — EOA credentials prefetch. Em paper/dry-run e sem chave,
    # CLOB fica read-only (post_order cairá em NotImplementedError, que o
    # CopyEngine converte em skip_reason=LIVE_NOT_WIRED).
    clob = CLOBClient()
    if settings.config.executor.mode == "live" and settings.env.private_key:
        try:
            signed = await prefetch_credentials(
                private_key=settings.env.private_key,
                funder=settings.env.funder_address,
                signature_type=settings.env.signature_type,
            )
            clob = CLOBClient(
                signed_client=signed.standard,
                neg_risk_signed_client=signed.neg_risk,
            )
        except Exception as exc:  # noqa: BLE001
            notifier.notify(f"🚨 EOA auth prefetch falhou: {exc!r}")
            log.critical("eoa_prefetch_failed", err=repr(exc))
            raise

    # --- pool, scanner & tracker -----------------------------------------
    wallet_pool = WalletPool(settings.config.scanner, db_path=DEFAULT_DB_PATH)
    active_wallets: set[str] = set()
    wallet_scores: dict[str, float] = {}
    rtds = RTDSClient(active_wallets)
    signal_queue: asyncio.Queue[TradeSignal] = asyncio.Queue(maxsize=1000)
    monitor = TradeMonitor(
        cfg=settings.config.tracker,
        data_client=data_client, gamma=gamma, ws_client=rtds,
        wallet_scores=wallet_scores, queue=signal_queue,
        conn=shared_conn, state=state,
    )
    scanner = Scanner(
        cfg=settings.config.scanner,
        data_client=data_client,
        pool=wallet_pool,
        active_wallets=active_wallets,
        wallet_scores=wallet_scores,
        state=state,
    )

    # --- balance cache (Phase 3/5) --------------------------------------
    # Em live com funder_address real, usa fetcher on-chain (web3 + USDC.e
    # Polygon). Sem funder → mantém stub com max_portfolio_usd pra não
    # bloquear paper/dry-run.
    funder = settings.env.funder_address
    if settings.config.executor.mode == "live" and funder:
        balance_fetcher = USDCBalanceFetcher(funder)
    else:
        async def balance_fetcher() -> float:  # type: ignore[misc]
            return settings.config.executor.max_portfolio_usd

    balance_cache = BalanceCache(balance_fetcher)

    # --- executor ---------------------------------------------------------
    risk = RiskManager(settings.config.executor)

    async def on_event(kind: str, signal: TradeSignal, trade: CopyTrade | None) -> None:
        if kind == "executed" and trade is not None:
            notifier.notify_trade(signal, trade)
        elif kind == "skipped":
            notifier.notify_skip(signal, signal.skip_reason or "unknown")

    # RiskState vivo — task assíncrona refaz snapshot a cada 15s a partir
    # de balance_cache + bot_positions. Elimina o curto-circuito do mock
    # zerado que bloqueava tudo por PORTFOLIO_CAP.
    from src.core.models import RiskState as _RS
    live_state: dict[str, _RS] = {
        "rs": await build_risk_state(balance_cache=balance_cache, conn=shared_conn)
    }

    async def refresh_risk_state() -> None:
        while not shutdown.is_set():
            try:
                live_state["rs"] = await build_risk_state(
                    balance_cache=balance_cache, conn=shared_conn,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("risk_state_refresh_failed", err=repr(exc))
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                pass

    engine = CopyEngine(
        cfg=settings.config.executor, clob=clob, gamma=gamma, risk=risk,
        queue=signal_queue, state=state,
        risk_state_provider=lambda: live_state["rs"],
        on_event=on_event, conn=shared_conn,
    )

    # --- graceful shutdown ------------------------------------------------
    shutdown = asyncio.Event()

    def _request_shutdown(signum: int, _frame: Any) -> None:
        log.warning("shutdown_requested", signal=signum)
        shutdown.set()

    # Windows suporta apenas SIGINT e SIGBREAK via signal; Linux suporta SIGTERM.
    for sig in (signal.SIGINT,) + ((signal.SIGTERM,) if sys.platform != "win32" else ()):
        try:
            signal.signal(sig, _request_shutdown)
        except (ValueError, OSError):
            pass

    tasks = [
        asyncio.create_task(heartbeat_loop(shutdown), name="heartbeat"),
        asyncio.create_task(scanner.run_loop(), name="scanner"),
        asyncio.create_task(monitor.run_websocket(), name="tracker-ws"),
        asyncio.create_task(engine.run_loop(), name="copy-engine"),
        asyncio.create_task(balance_cache.run_loop(), name="balance-cache"),
        asyncio.create_task(refresh_risk_state(), name="risk-state-refresh"),
    ]

    notifier.notify(
        f"🟢 PolyTrader online | mode={settings.config.executor.mode} "
        f"| vps={settings.env.vps_provider}"
    )

    await shutdown.wait()
    log.info("shutting_down")

    await rtds.close()
    await scanner.stop()
    await balance_cache.stop()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await shared_conn.close()
    await close_http_client()
    notifier.notify("🔴 PolyTrader offline")
    log.info("shutdown_complete")


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
