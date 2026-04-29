"""PolyTrader — entry point.

Orquestra scanner + tracker + executor + notifier com graceful shutdown.
Inicializa schema (idempotente), configura logging, e entra em asyncio.gather.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
from datetime import datetime, timezone
from typing import Any

import aiosqlite
import uvicorn

from src.api.auth import prefetch_credentials
from src.api.balance import USDCBalanceFetcher
from src.api.clob_client import CLOBClient
from src.api.data_client import DataAPIClient
from src.api.gamma_client import GammaAPIClient
from src.api.http import close_http_client, prewarm_connections
from src.api.startup_checks import check_geoblock, latency_baseline
from src.api.websocket_client import RTDSClient
from src.arbitrage.ctf_client import CTFClient
from src.arbitrage.executor import ArbExecutor
from src.arbitrage.models import ArbExecution, ArbOpportunity
from src.arbitrage.scanner import ArbScanner
from src.core.config import get_settings
from src.core.database import DEFAULT_DB_PATH, init_database, open_shared_connection
from src.core.logger import configure_logging, get_logger
from src.core.models import CopyTrade, TradeSignal
from src.core.state import InMemoryState
from src.dashboard.app import build_app as build_dashboard
from src.executor.balance_cache import BalanceCache
from src.executor.copy_engine import CopyEngine
from src.executor.position_sync import reconcile_bot_positions
from src.executor.price_updater import price_update_loop
from src.executor.resolution_watcher import resolution_check_loop
from src.executor.risk_manager import RiskManager
from src.executor.risk_state import build_risk_state
from src.executor.stale_cleanup import stale_position_cleanup_loop
from src.notifier.daily_summary import daily_summary_loop
from src.notifier.telegram import TelegramNotifier
from src.scanner.scanner import Scanner
from src.scanner.wallet_pool import WalletPool
from src.tracker.trade_monitor import TradeMonitor

log = get_logger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 60
KILL_SWITCH_PATH = "/opt/polytrader/KILL_SWITCH"  # UNIX standard; no Windows é ignorado

# sd_notify — habilitado apenas em Linux com systemd. Em Windows/dev o
# ImportError é engolido; o bot vira no-op e segue vivo.
try:
    from systemd.daemon import notify as _sd_notify  # type: ignore[import-not-found]
    _HAS_SYSTEMD = True
except ImportError:
    _HAS_SYSTEMD = False

    def _sd_notify(_msg: str) -> int:  # fallback
        return 0


async def latency_monitor_loop(
    shutdown: asyncio.Event,
    *,
    interval_s: int,
    threshold_ms: int,
    notifier: "TelegramNotifier",
) -> None:
    """Roda latency_baseline periodicamente; alerta se qualquer rota
    exceder o threshold de forma persistente (2 probes consecutivos)."""
    consecutive_high: dict[str, int] = {}
    while not shutdown.is_set():
        try:
            rtts = await latency_baseline()
        except Exception as exc:  # noqa: BLE001
            log.warning("latency_monitor_probe_failed", err=repr(exc))
            rtts = {}
        for target, rtt in rtts.items():
            if rtt is None or rtt <= threshold_ms:
                consecutive_high[target] = 0
                continue
            consecutive_high[target] = consecutive_high.get(target, 0) + 1
            if consecutive_high[target] >= 2:
                notifier.notify(
                    f"⚠️ Latência alta em {target}: {rtt:.0f}ms "
                    f"(threshold {threshold_ms}ms, probes consecutivos {consecutive_high[target]})"
                )
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass


async def heartbeat_loop(shutdown: asyncio.Event) -> None:
    while not shutdown.is_set():
        log.info("heartbeat", systemd=_HAS_SYSTEMD)
        if _HAS_SYSTEMD:
            _sd_notify("WATCHDOG=1")
        # H7 — kill-switch físico. Operador faz `touch /opt/polytrader/KILL_SWITCH`
        # via SSH/SCP e o bot para no próximo heartbeat (<= 60s).
        if os.path.exists(KILL_SWITCH_PATH):
            log.critical("kill_switch_detected", path=KILL_SWITCH_PATH)
            shutdown.set()
            return
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def amain() -> None:
    settings = get_settings()
    configure_logging(settings.env.log_level)
    started_at = datetime.now(timezone.utc)

    # M8 — fail-fast: require_auth=True + secret vazio = dashboard 503 eterno.
    if settings.config.dashboard.require_auth and not settings.env.dashboard_secret:
        log.critical(
            "dashboard_secret_missing",
            msg="require_auth=true mas DASHBOARD_SECRET vazio. Configure .env.",
        )
        sys.exit(1)
    # Captura o event loop realmente em uso (não um new_event_loop artificial).
    _running_loop = asyncio.get_running_loop()
    log.info(
        "startup",
        mode=settings.config.executor.mode,
        vps=f"{settings.env.vps_provider}-{settings.env.vps_location}",
        event_loop=f"{type(_running_loop).__module__}.{type(_running_loop).__name__}",
    )

    await init_database()

    # --- shared SQLite connection (Phase 0 — preserva ganho iter 6) ------
    # open_shared_connection aplica synchronous=NORMAL + cache 16MB + WAL tuning.
    shared_conn: aiosqlite.Connection = await open_shared_connection(DEFAULT_DB_PATH)
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

    # Pre-warm connection pool — estabelece TCP+TLS em cada endpoint
    # ANTES do primeiro signal virar ordem. Elimina cold-start spike.
    await prewarm_connections()

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
    # PAPER/DRY-RUN: bot não posta on-chain → wallet vazia na blockchain.
    # Reconcile compararia DB cheio com on-chain vazio e fecharia TODAS as
    # posições como "fantasma" (size=0, sem realized_pnl, sem close_reason).
    # Isso destruía o tracking de PnL no paper. Só rodar em LIVE.
    if settings.config.executor.mode == "live" and settings.env.funder_address:
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
        log.info("position_sync_skipped",
                 reason="paper_mode" if settings.config.executor.mode != "live"
                                     else "funder_address_empty")

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
    wallet_portfolios: dict[str, float] = {}
    rtds = RTDSClient(active_wallets)
    signal_queue: asyncio.Queue[TradeSignal] = asyncio.Queue(maxsize=1000)
    monitor = TradeMonitor(
        cfg=settings.config.tracker,
        data_client=data_client, gamma=gamma, ws_client=rtds,
        wallet_scores=wallet_scores, queue=signal_queue,
        conn=shared_conn, state=state,
        wallet_portfolios=wallet_portfolios,
    )
    scanner = Scanner(
        cfg=settings.config.scanner,
        data_client=data_client,
        pool=wallet_pool,
        active_wallets=active_wallets,
        wallet_scores=wallet_scores,
        state=state,
        wallet_portfolios=wallet_portfolios,
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

    async def reconcile_loop() -> None:
        """Reconcilia bot_positions vs on-chain a cada 30min (live-only).

        Startup já faz 1 reconcile; esta task cobre drift cumulativo de
        apply_fill falhos silenciosamente ou fills vindos do user-channel
        do CLOB que não foram capturados pelo pipeline local.
        """
        if settings.config.executor.mode != "live":
            return
        if not settings.env.funder_address:
            return
        interval = 30 * 60  # 30 minutos
        while not shutdown.is_set():
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            if shutdown.is_set():
                break
            try:
                stats = await reconcile_bot_positions(
                    data_client=data_client,
                    wallet_address=settings.env.funder_address,
                    conn=shared_conn,
                    state=state,
                )
                if stats["added"] or stats["updated"] or stats["closed"]:
                    notifier.notify(
                        f"🔄 Periodic reconcile: +{stats['added']} / "
                        f"~{stats['updated']} / ✕{stats['closed']}"
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("periodic_reconcile_failed", err=repr(exc))

    # Estado de alerta do balance cache — evita spam de notificação
    # (dispara 1x ao envelhecer, 1x ao recuperar).
    balance_stale_alerted = {"v": False}

    async def refresh_risk_state() -> None:
        while not shutdown.is_set():
            try:
                live_state["rs"] = await build_risk_state(
                    balance_cache=balance_cache, conn=shared_conn,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("risk_state_refresh_failed", err=repr(exc))

            # LOW 3 — balance cache stale = portfolio vira 0 → todos
            # trades viram PORTFOLIO_CAP sem aviso. Alerta assim que
            # detectado e reseta no recovery.
            age = balance_cache.age()
            stale = (not balance_cache.is_fresh) or (
                age is not None and age.total_seconds() > 300
            )
            if stale and not balance_stale_alerted["v"]:
                age_s = f"{age.total_seconds():.0f}s" if age else "never"
                notifier.notify(
                    f"🚨 Balance cache estagnado (age={age_s}). "
                    "RiskState vai zerar portfolio e bloquear trades por PORTFOLIO_CAP."
                )
                balance_stale_alerted["v"] = True
            elif not stale and balance_stale_alerted["v"]:
                notifier.notify("✅ Balance cache recuperado.")
                balance_stale_alerted["v"] = False

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

    # --- arbitrage engine (Track A) — RODA EM PARALELO ao copy-trader -----
    # Capital separado (cfg.arbitrage.max_capital_usd), risk profile próprio.
    # Edge matemático: YES+NO < 1 → mergePositions no CTF devolve $1.
    arb_queue: asyncio.Queue[ArbOpportunity] = asyncio.Queue(maxsize=200)
    arb_scanner: ArbScanner | None = None
    arb_executor: ArbExecutor | None = None
    if settings.config.arbitrage.enabled:
        # CTF client só é necessário se mode=live + auto_merge.
        ctf: CTFClient | None = None
        if (
            settings.config.arbitrage.mode == "live"
            and settings.config.arbitrage.auto_merge
        ):
            if not settings.env.private_key:
                log.critical("arb_live_requires_private_key")
                notifier.notify("🚨 Arb LIVE pediu PRIVATE_KEY mas .env vazio.")
                sys.exit(1)
            ctf = CTFClient(
                rpc_url=settings.env.polygon_rpc_url,
                ctf_address=settings.env.ctf_contract_address,
                usdc_address=settings.env.usdc_contract_address,
                private_key=settings.env.private_key,
                funder_address=settings.env.funder_address or None,
            )
        arb_scanner = ArbScanner(
            cfg=settings.config.arbitrage, gamma=gamma, clob=clob,
            queue=arb_queue, conn=shared_conn,
        )

        async def on_arb_event(
            kind: str, op: ArbOpportunity, ex: ArbExecution | None,
        ) -> None:
            if kind == "arb_executed" and ex is not None:
                emoji = "💎" if ex.status == "completed" else "⚠️"
                pnl = ex.realized_pnl_usd if ex.realized_pnl_usd is not None else 0.0
                notifier.notify(
                    f"{emoji} ARB {ex.status.upper()} | edge_net={op.edge_net_pct:.4f} "
                    f"| pnl={pnl:+.4f} USDC | {(op.market_title or '')[:50]}"
                )

        arb_executor = ArbExecutor(
            cfg=settings.config.arbitrage, clob=clob, gamma=gamma, ctf=ctf,
            queue=arb_queue, conn=shared_conn, on_event=on_arb_event,
        )
        log.info(
            "arb_engine_wired",
            mode=settings.config.arbitrage.mode,
            capital=settings.config.arbitrage.max_capital_usd,
            min_edge=settings.config.arbitrage.min_edge_pct,
        )
    else:
        log.info("arb_engine_disabled")

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

    # Dashboard FastAPI — sobe em task separada com uvicorn.Server
    dashboard_app = build_dashboard(
        secret=settings.env.dashboard_secret,
        dashboard_user=settings.env.dashboard_user,
        state=state, balance_cache=balance_cache, shared_conn=shared_conn,
        started_at=started_at, mode=settings.config.executor.mode,
        vps_location=settings.env.vps_location,
        risk_manager=risk,
    )
    uv_config = uvicorn.Config(
        dashboard_app,
        host=settings.config.dashboard.host,
        port=settings.config.dashboard.port,
        log_level="warning", access_log=False, lifespan="off",
    )
    dashboard_server = uvicorn.Server(uv_config)

    # Notifier de circuit-breaker — CopyEngine emite `risk_halt` via on_event
    original_on_event = on_event

    async def on_event_with_halt(kind: str, sig: TradeSignal, tr: CopyTrade | None) -> None:
        if kind == "risk_halt":
            notifier.notify(
                f"🚨 CIRCUIT BREAKER: {risk.halt_reason}. Trading congelado."
            )
        await original_on_event(kind, sig, tr)

    engine._on_event = on_event_with_halt  # re-wire após build

    tasks = [
        asyncio.create_task(heartbeat_loop(shutdown), name="heartbeat"),
        asyncio.create_task(scanner.run_loop(), name="scanner"),
        asyncio.create_task(monitor.run_websocket(), name="tracker-ws"),
        asyncio.create_task(monitor.run_polling(active_wallets), name="tracker-polling"),
        asyncio.create_task(engine.run_loop(), name="copy-engine"),
        asyncio.create_task(balance_cache.run_loop(), name="balance-cache"),
        asyncio.create_task(refresh_risk_state(), name="risk-state-refresh"),
        asyncio.create_task(reconcile_loop(), name="reconcile-loop"),
        asyncio.create_task(
            latency_monitor_loop(
                shutdown,
                interval_s=settings.env.latency_probe_interval_seconds,
                threshold_ms=settings.env.latency_alert_threshold_ms,
                notifier=notifier,
            ),
            name="latency-monitor",
        ),
        asyncio.create_task(dashboard_server.serve(), name="dashboard"),
        asyncio.create_task(
            price_update_loop(shutdown=shutdown, conn=shared_conn, clob=clob),
            name="price-updater",
        ),
        asyncio.create_task(
            resolution_check_loop(shutdown=shutdown, conn=shared_conn, gamma=gamma),
            name="resolution-watcher",
        ),
        asyncio.create_task(
            stale_position_cleanup_loop(
                shutdown=shutdown, cfg=settings.config.executor,
                conn=shared_conn, signal_queue=signal_queue,
            ),
            name="stale-cleanup",
        ),
        asyncio.create_task(
            daily_summary_loop(
                shutdown=shutdown,
                hour_utc=settings.config.notifier.daily_summary_hour,
                conn=shared_conn, balance_cache=balance_cache,
                notifier=notifier, started_at=started_at,
                mode=settings.config.executor.mode,
            ),
            name="daily-summary",
        ),
    ]

    # Track A — arb tasks (apenas se cfg.arbitrage.enabled).
    if arb_scanner is not None and arb_executor is not None:
        tasks.append(asyncio.create_task(
            arb_scanner.run_loop(shutdown), name="arb-scanner",
        ))
        tasks.append(asyncio.create_task(
            arb_executor.run_loop(shutdown), name="arb-executor",
        ))

    notifier.notify(
        f"🟢 PolyTrader online | mode={settings.config.executor.mode} "
        f"| vps={settings.env.vps_provider}"
    )

    await shutdown.wait()
    log.info("shutting_down")

    await rtds.close()
    await scanner.stop()
    await balance_cache.stop()
    dashboard_server.should_exit = True
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    # HIGH 1 — cancela GTC orders abertas ANTES de fechar a conexão para
    # evitar ordens-zumbi executando sem supervisão após systemctl stop.
    if settings.config.executor.mode == "live" and clob._signed is not None:
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, clob._signed.cancel_all)
            log.info("open_orders_cancelled_on_shutdown")
        except Exception as exc:  # noqa: BLE001 — não bloquear shutdown
            log.error("cancel_all_failed_on_shutdown", err=repr(exc))

    await shared_conn.close()
    await close_http_client()
    notifier.notify("🔴 PolyTrader offline")
    log.info("shutdown_complete")


def main() -> None:
    # uvloop: event loop em C, 2-4× mais rápido que asyncio default em I/O.
    # Só instala em Linux/Mac (sys_platform != win32 marker já no pyproject).
    try:
        import uvloop  # type: ignore[import-not-found]
        uvloop.install()
    except ImportError:
        pass  # Windows / dev local cai no default asyncio sem quebrar
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
