"""Detecta resolução de mercados e fecha bot_positions abertas.

Na Polymarket, cada mercado tem 2 outcomes (Yes/No). Na resolução:
    * Token vencedor → 1.00 USDC
    * Token perdedor → 0.00 USDC

Gamma API expõe isso via:
    market.closed: bool
    market.outcomePrices: ['1', '0']  ou  ['0', '1']
    market.outcomes: ['Yes', 'No']

Fluxo:
  1. SELECT bot_positions abertas → group por condition_id
  2. Para cada cid, GET /markets?condition_ids=X da Gamma
  3. Se closed=true → determinar outcome vencedor
     * Se nossa posição é no token vencedor: realized_pnl = (1 - avg_entry) * size
     * Se perdedor:                          realized_pnl = (0 - avg_entry) * size = -avg_entry * size
  4. UPDATE is_open=0, closed_at, realized_pnl, current_price
"""
from __future__ import annotations

import asyncio
import json

import aiosqlite

from src.api.gamma_client import GammaAPIClient
from src.core.logger import get_logger

log = get_logger(__name__)

RESOLUTION_CHECK_INTERVAL_SECONDS = 300  # 5min


def _parse_outcome_prices(market: dict) -> list[float] | None:
    """outcomePrices vem como string JSON ou lista. Normaliza."""
    raw = market.get("outcomePrices") or market.get("outcome_prices")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if isinstance(raw, list):
        try:
            return [float(p) for p in raw]
        except (TypeError, ValueError):
            return None
    return None


async def _resolve_position(
    conn: aiosqlite.Connection,
    gamma: GammaAPIClient,
    position_id: int,
    condition_id: str,
    token_id: str,
    outcome: str,
    size: float,
    avg_entry: float,
) -> str | None:
    """Retorna string de status ('won'/'lost'/'still_open') ou None se erro."""
    try:
        market = await gamma.get_market(condition_id, force_refresh=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("resolution_gamma_failed",
                    cid=condition_id[:12], err=repr(exc))
        return None

    closed = bool(market.get("closed")) or bool(market.get("resolved"))
    if not closed:
        return "still_open"

    prices = _parse_outcome_prices(market)
    outcomes = market.get("outcomes")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except json.JSONDecodeError:
            outcomes = None
    if not prices or not isinstance(outcomes, list):
        log.warning("resolution_unable_to_parse",
                    cid=condition_id[:12],
                    prices=prices, outcomes=outcomes)
        return None

    # Descobre qual outcome corresponde ao nosso token.
    # outcomes é ['Yes','No'], prices é [1.0, 0.0] ou similar.
    # Nossa posição tem `outcome` ('Yes' ou 'No').
    try:
        idx = next(i for i, o in enumerate(outcomes)
                   if str(o).lower() == str(outcome).lower())
    except StopIteration:
        log.warning("resolution_outcome_not_found",
                    cid=condition_id[:12], outcome=outcome, outcomes=outcomes)
        return None

    resolution_price = float(prices[idx])
    realized_pnl = (resolution_price - avg_entry) * size
    won = resolution_price > avg_entry

    now = "datetime('now')"
    await conn.execute(
        f"UPDATE bot_positions SET is_open=0, closed_at={now}, "
        "updated_at=" + now + ", current_price=?, realized_pnl=?, "
        "unrealized_pnl=0 WHERE id=?",
        (resolution_price, realized_pnl, position_id),
    )
    log.info(
        "position_resolved",
        cid=condition_id[:12], outcome=outcome,
        resolution_price=resolution_price, entry=avg_entry,
        size=size, realized_pnl=realized_pnl, won=won,
    )
    return "won" if won else "lost"


async def check_resolutions_once(
    conn: aiosqlite.Connection,
    gamma: GammaAPIClient,
) -> dict[str, int]:
    stats = {"checked": 0, "won": 0, "lost": 0, "still_open": 0, "errors": 0}
    async with conn.execute(
        "SELECT id, condition_id, token_id, outcome, size, avg_entry_price "
        "FROM bot_positions WHERE is_open=1"
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        stats["checked"] += 1
        pid, cid, tok, outcome, size, entry = row[0], row[1], row[2], row[3], float(row[4]), float(row[5])
        status = await _resolve_position(conn, gamma, pid, cid, tok, outcome, size, entry)
        if status == "won":
            stats["won"] += 1
        elif status == "lost":
            stats["lost"] += 1
        elif status == "still_open":
            stats["still_open"] += 1
        else:
            stats["errors"] += 1
    if stats["won"] or stats["lost"]:
        await conn.commit()
    return stats


async def resolution_check_loop(
    *,
    shutdown: asyncio.Event,
    conn: aiosqlite.Connection,
    gamma: GammaAPIClient,
) -> None:
    while not shutdown.is_set():
        try:
            stats = await check_resolutions_once(conn, gamma)
            if stats["checked"]:
                log.info("resolution_check_tick", **stats)
        except Exception as exc:  # noqa: BLE001
            log.error("resolution_loop_crash", err=repr(exc))
        try:
            await asyncio.wait_for(
                shutdown.wait(), timeout=RESOLUTION_CHECK_INTERVAL_SECONDS,
            )
        except asyncio.TimeoutError:
            pass
