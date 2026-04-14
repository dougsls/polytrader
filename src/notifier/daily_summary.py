"""Daily summary — dispara relatório no Telegram às HH:00 UTC configurado.

Coletamos do SQLite + balance_cache:
    - PnL realizado (últimas 24h)
    - PnL unrealized (posições abertas)
    - Uptime em horas
    - Top 3 e Worst 3 positions por unrealized_pnl
    - Número de sinais detectados / executados / skippeds no dia
    - Saldo USDC on-chain
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import aiosqlite

from src.core.logger import get_logger
from src.executor.balance_cache import BalanceCache
from src.notifier.telegram import TelegramNotifier

log = get_logger(__name__)


def _seconds_until_next(hour_utc: int, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    target = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def build_summary_text(
    *,
    conn: aiosqlite.Connection,
    balance_cache: BalanceCache,
    started_at: datetime,
    mode: str,
) -> str:
    now = datetime.now(timezone.utc)
    uptime_h = (now - started_at).total_seconds() / 3600.0
    day_ago = (now - timedelta(hours=24)).isoformat()

    async with conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(realized_pnl), 0) "
        "FROM bot_positions WHERE closed_at >= ?",
        (day_ago,),
    ) as cur:
        row = await cur.fetchone()
    closed_count = int(row[0]) if row else 0
    realized_24h = float(row[1]) if row else 0.0

    async with conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(unrealized_pnl), 0) "
        "FROM bot_positions WHERE is_open=1"
    ) as cur:
        row = await cur.fetchone()
    open_count = int(row[0]) if row else 0
    unrealized = float(row[1]) if row else 0.0

    async with conn.execute(
        "SELECT status, COUNT(*) FROM trade_signals WHERE detected_at >= ? GROUP BY status",
        (day_ago,),
    ) as cur:
        counts = {r[0]: int(r[1]) for r in await cur.fetchall()}

    async with conn.execute(
        "SELECT market_title, unrealized_pnl FROM bot_positions "
        "WHERE is_open=1 ORDER BY unrealized_pnl DESC LIMIT 3"
    ) as cur:
        top = await cur.fetchall()
    async with conn.execute(
        "SELECT market_title, unrealized_pnl FROM bot_positions "
        "WHERE is_open=1 ORDER BY unrealized_pnl ASC LIMIT 3"
    ) as cur:
        worst = await cur.fetchall()

    top_lines = "\n".join(f"  +{r[1]:.2f} {r[0][:40]}" for r in top) or "  —"
    worst_lines = "\n".join(f"  {r[1]:.2f} {r[0][:40]}" for r in worst) or "  —"

    return (
        f"📊 <b>Daily Summary — {now.strftime('%Y-%m-%d %H:%M UTC')}</b>\n"
        f"Mode: {mode} | Uptime: {uptime_h:.1f}h\n"
        f"Balance USDC: {balance_cache.balance_usdc:,.2f} "
        f"({'fresh' if balance_cache.is_fresh else 'STALE'})\n\n"
        f"Realized PnL 24h: {realized_24h:+.2f} ({closed_count} closed)\n"
        f"Unrealized: {unrealized:+.2f} ({open_count} open)\n\n"
        f"Signals 24h: executed={counts.get('executed', 0)} "
        f"skipped={counts.get('skipped', 0)} pending={counts.get('pending', 0)}\n\n"
        f"Top 3 open:\n{top_lines}\n\n"
        f"Worst 3 open:\n{worst_lines}"
    )


async def daily_summary_loop(
    *,
    shutdown: asyncio.Event,
    hour_utc: int,
    conn: aiosqlite.Connection,
    balance_cache: BalanceCache,
    notifier: TelegramNotifier,
    started_at: datetime,
    mode: str,
) -> None:
    while not shutdown.is_set():
        wait_s = _seconds_until_next(hour_utc)
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=wait_s)
            return  # shutdown antes do tick
        except asyncio.TimeoutError:
            pass
        try:
            text = await build_summary_text(
                conn=conn, balance_cache=balance_cache,
                started_at=started_at, mode=mode,
            )
            notifier.notify(text)
            log.info("daily_summary_sent")
        except Exception as exc:  # noqa: BLE001
            log.error("daily_summary_failed", err=repr(exc))
