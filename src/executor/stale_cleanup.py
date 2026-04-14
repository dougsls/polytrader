"""H4 — Stale position cleanup.

A Regra 2 (Exit Syncing) só dispara SELL quando a baleia vende. Se a
baleia esquece a posição (ou sai sem nós detectarmos), capital fica
preso até resolução do mercado. Esta task percorre `bot_positions`
abertas e emite um `TradeSignal` SELL sintético quando a posição
ultrapassa `max_position_age_hours`.

Roda a cada 5 minutos, só em mode=live|paper.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import aiosqlite

from src.core.config import ExecutorConfig
from src.core.logger import get_logger
from src.core.models import TradeSignal

log = get_logger(__name__)

CHECK_INTERVAL_SECONDS = 5 * 60


async def _find_stale(
    conn: aiosqlite.Connection, max_age_hours: float,
) -> list[tuple[str, str, str, str, float, float]]:
    """Retorna (condition_id, token_id, title, outcome, size, avg_price)."""
    cutoff = datetime.now(timezone.utc).timestamp() - (max_age_hours * 3600)
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    async with conn.execute(
        "SELECT condition_id, token_id, market_title, outcome, "
        "       size, avg_entry_price "
        "FROM bot_positions WHERE is_open=1 AND opened_at < ?",
        (cutoff_iso,),
    ) as cur:
        rows = await cur.fetchall()
    return [(r[0], r[1], r[2], r[3], float(r[4]), float(r[5])) for r in rows]


def _synthetic_sell_signal(
    condition_id: str, token_id: str, title: str, outcome: str,
    size: float, avg_price: float,
) -> TradeSignal:
    """SELL sintético (wallet_score=1.0 p/ passar risk gate de confidence)."""
    return TradeSignal.model_construct(
        id=f"stale-{uuid.uuid4().hex[:12]}",
        wallet_address="__stale_cleanup__",
        wallet_score=1.0,  # Bypass min_confidence_score
        condition_id=condition_id, token_id=token_id,
        side="SELL", size=size, price=avg_price,
        usd_value=size * avg_price,
        market_title=title, outcome=outcome,
        market_end_date=None, hours_to_resolution=None,
        detected_at=datetime.now(timezone.utc),
        source="polling", status="pending", skip_reason=None,
    )


async def stale_position_cleanup_loop(
    *,
    shutdown: asyncio.Event,
    cfg: ExecutorConfig,
    conn: aiosqlite.Connection,
    signal_queue: asyncio.Queue[TradeSignal],
) -> None:
    if cfg.mode == "dry-run":
        return
    while not shutdown.is_set():
        try:
            await asyncio.wait_for(
                shutdown.wait(), timeout=CHECK_INTERVAL_SECONDS,
            )
            return
        except asyncio.TimeoutError:
            pass
        try:
            stale = await _find_stale(conn, cfg.max_position_age_hours)
            for cid, tok, title, outcome, size, avg_price in stale:
                sig = _synthetic_sell_signal(cid, tok, title, outcome, size, avg_price)
                try:
                    signal_queue.put_nowait(sig)
                    log.warning(
                        "stale_position_auto_sell",
                        token_id=tok, size=size, age_limit_h=cfg.max_position_age_hours,
                    )
                except asyncio.QueueFull:
                    log.warning("stale_cleanup_queue_full", token_id=tok)
        except Exception as exc:  # noqa: BLE001
            log.error("stale_cleanup_failed", err=repr(exc))
