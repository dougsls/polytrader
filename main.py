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

from src.api.clob_client import CLOBClient
from src.api.data_client import DataAPIClient
from src.api.gamma_client import GammaAPIClient
from src.api.http import close_http_client
from src.api.websocket_client import RTDSClient
from src.core.config import get_settings
from src.core.database import DEFAULT_DB_PATH, init_database
from src.core.logger import configure_logging, get_logger
from src.core.models import CopyTrade, RiskState, TradeSignal
from src.core.state import InMemoryState
from src.executor.balance_cache import BalanceCache
from src.executor.copy_engine import CopyEngine
from src.executor.risk_manager import RiskManager
from src.notifier.telegram import TelegramNotifier
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


def _default_state() -> RiskState:
    """Estado inicial neutro — Fase 5 (dashboard/DB aggregator) popula de verdade."""
    return RiskState(
        total_portfolio_value=0.0,
        total_invested=0.0,
        total_unrealized_pnl=0.0,
        total_realized_pnl=0.0,
        daily_pnl=0.0,
        max_drawdown=0.0,
        current_drawdown=0.0,
        open_positions=0,
    )


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
    # UMA conexão pela vida do processo. Propagada para o tracker, que
    # propaga para detect_signal. Nunca fazer fallback para get_connection()
    # dentro do hot path — mataria o ganho de 56×.
    shared_conn: aiosqlite.Connection = await aiosqlite.connect(DEFAULT_DB_PATH)
    log.info("shared_conn_opened")

    # --- in-memory state cache (Phase 4) ---------------------------------
    state = InMemoryState()
    await state.reload_from_db(conn=shared_conn)

    # --- clients ----------------------------------------------------------
    data_client = DataAPIClient()
    gamma = GammaAPIClient()
    clob = CLOBClient()  # read-only até injetar signer (EOA prefetch na Fase 5)

    # --- notifier ---------------------------------------------------------
    notifier = TelegramNotifier(
        token=settings.env.telegram_bot_token,
        chat_id=settings.env.telegram_chat_id,
    )

    # --- pool & tracker ---------------------------------------------------
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

    # --- balance cache (Phase 3) -----------------------------------------
    # Stub: em live, substitua por chamada real ao Polygon RPC / CLOB balance.
    async def _fetch_balance_stub() -> float:
        return settings.config.executor.max_portfolio_usd

    balance_cache = BalanceCache(_fetch_balance_stub)

    # --- executor ---------------------------------------------------------
    risk = RiskManager(settings.config.executor)

    async def on_event(kind: str, signal: TradeSignal, trade: CopyTrade | None) -> None:
        if kind == "executed" and trade is not None:
            notifier.notify_trade(signal, trade)
        elif kind == "skipped":
            notifier.notify_skip(signal, signal.skip_reason or "unknown")

    engine = CopyEngine(
        cfg=settings.config.executor, clob=clob, gamma=gamma, risk=risk,
        queue=signal_queue, state_provider=_default_state, on_event=on_event,
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
        asyncio.create_task(monitor.run_websocket(), name="tracker-ws"),
        asyncio.create_task(engine.run_loop(), name="copy-engine"),
        asyncio.create_task(balance_cache.run_loop(), name="balance-cache"),
    ]

    notifier.notify(
        f"🟢 PolyTrader online | mode={settings.config.executor.mode} "
        f"| vps={settings.env.vps_provider}"
    )

    await shutdown.wait()
    log.info("shutting_down")

    await rtds.close()
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
