"""Loop periódico de snapshots da demo (banca + readiness score).

Roda em paralelo aos outros loops do bot. A cada N segundos:
  - Calcula bank snapshot (cash, invested, market value, PnL, DD).
  - Persiste em demo_bank_snapshots.
  - Calcula readiness score + persiste em demo_readiness_snapshots.

Sem isso, o equity curve fica sem pontos e o readiness score só é
calculado quando o dashboard é aberto.
"""
from __future__ import annotations

import asyncio
from typing import Any

import aiosqlite

from src.core.logger import get_logger
from src.core.state import InMemoryState
from src.dashboard.demo_aggregators import readiness_overview
from src.dashboard.demo_persistence import (
    record_bank_snapshot,
    record_readiness,
)
from src.dashboard.readiness_score import compute_readiness
from src.executor.balance_cache import BalanceCache

log = get_logger(__name__)

SNAPSHOT_INTERVAL_SECONDS = 60  # 1 ponto por minuto = 1440 pontos em 24h


async def demo_snapshot_loop(
    *,
    shutdown: asyncio.Event,
    conn: aiosqlite.Connection,
    starting_bank_usd: float,
    state: InMemoryState,
    balance_cache: BalanceCache,
    queue: Any,  # asyncio.Queue (Any por compat)
) -> None:
    """Tick a cada SNAPSHOT_INTERVAL_SECONDS — só roda em modo paper/demo."""
    log.info("demo_snapshot_loop_start", interval_s=SNAPSHOT_INTERVAL_SECONDS)
    # Espera 5s no startup pra DB + state estabilizarem.
    try:
        await asyncio.wait_for(shutdown.wait(), timeout=5.0)
        return
    except asyncio.TimeoutError:
        pass

    while not shutdown.is_set():
        try:
            # Pull cards atuais
            overview = await readiness_overview(
                conn, starting_bank_usd=starting_bank_usd,
            )
            # Counters via signals_24h + journal_24h
            sig24 = overview.get("signals_24h") or {}
            jr24 = overview.get("journal_24h") or {}
            counters = {
                "total": sig24.get("total", 0),
                "executed": jr24.get("executed", 0),
                "skipped": jr24.get("skipped", 0),
                "partial": jr24.get("partial", 0),
                "dropped": 0,
                "wins": overview.get("wins", 0),
                "losses": overview.get("losses", 0),
            }
            health = {
                "rtds_connected": True,  # estado real seria via metric/probe
                "polling_active": True,
                "geoblock_status": "ok",
                "queue_size": queue.qsize() if queue else 0,
            }
            await record_bank_snapshot(
                starting_bank_usd=starting_bank_usd,
                cash_available=overview.get("cash_available", 0.0),
                invested_usd=overview.get("invested_usd", 0.0),
                market_value_usd=overview.get("market_value_usd", 0.0),
                realized_pnl=overview.get("realized_pnl", 0.0),
                unrealized_pnl=overview.get("unrealized_pnl", 0.0),
                peak_bank=overview.get("peak_bank", starting_bank_usd),
                open_positions=overview.get("open_positions", 0),
                counters=counters,
                health=health,
                conn=conn,
            )
            # Readiness score snapshot
            score = await compute_readiness(
                conn, starting_bank_usd=starting_bank_usd,
            )
            await record_readiness(
                score=score.score,
                recommendation=score.recommendation,
                components=score.components,
                no_go_flags=score.no_go_flags,
                details=score.details,
                conn=conn,
            )
            log.debug(
                "demo_snapshot_recorded",
                score=score.score, recommendation=score.recommendation,
                bank=overview.get("bank_total"),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("demo_snapshot_failed", err=repr(exc)[:120])

        try:
            await asyncio.wait_for(
                shutdown.wait(), timeout=SNAPSHOT_INTERVAL_SECONDS,
            )
        except asyncio.TimeoutError:
            pass
