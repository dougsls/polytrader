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

RESOLUTION_CHECK_INTERVAL_SECONDS = 60  # 1min — captura inferência por preço rapidamente


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


async def _local_resolve_if_extreme(
    conn: aiosqlite.Connection, position_id: int, condition_id: str,
    outcome: str, size: float, avg_entry: float,
) -> str | None:
    """Fallback: usa DB local (current_price do price_updater + end_date do cache)
    pra inferir resolução quando Gamma não expõe mais o mercado."""
    from datetime import datetime, timezone
    async with conn.execute(
        "SELECT bp.current_price, m.end_date FROM bot_positions bp "
        "LEFT JOIN market_metadata_cache m ON m.condition_id = bp.condition_id "
        "WHERE bp.id=?", (position_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    cur_price = float(row[0]) if row[0] else 0.0
    end_iso = row[1]
    if not end_iso:
        return None
    try:
        end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None
    end_passed_min = (datetime.now(timezone.utc) - end_dt).total_seconds() / 60.0
    if end_passed_min < 2.0:
        return None
    if not (cur_price >= 0.99 or cur_price <= 0.01):
        return None
    # Inferido: preço em extremo + endDate passou
    resolution_price = 1.0 if cur_price >= 0.99 else 0.0
    realized_pnl = (resolution_price - avg_entry) * size
    won = resolution_price > avg_entry
    now = "datetime('now')"
    await conn.execute(
        f"UPDATE bot_positions SET is_open=0, closed_at={now}, "
        "updated_at=" + now + ", current_price=?, realized_pnl=?, "
        "unrealized_pnl=0, close_reason='resolved' WHERE id=?",
        (resolution_price, realized_pnl, position_id),
    )
    log.info("position_resolved_local_fallback",
             cid=condition_id[:12], outcome=outcome, cur_price=cur_price,
             resolution_price=resolution_price, end_passed_min=end_passed_min,
             realized_pnl=realized_pnl, won=won)
    return "won" if won else "lost"


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
        # Gamma 404 = mercado resolveu e saiu do filtro active=true. Usa
        # nosso DB (price_updater + cache endDate) pra inferir.
        log.info("resolution_gamma_not_found_trying_local",
                 cid=condition_id[:12], err=repr(exc)[:80])
        return await _local_resolve_if_extreme(
            conn, position_id, condition_id, outcome, size, avg_entry,
        )

    closed = bool(market.get("closed")) or bool(market.get("resolved"))

    # INFERÊNCIA POR PREÇO (mesmo com Gamma retornando closed=false).
    # Polymarket UMA oracle pode levar 15-60min pra settle após endDate.
    # Durante esse gap, Gamma: closed=false, lastTradePrice stale. MAS o
    # NOSSO current_price (via price_updater + CLOB /price) já mostra 1.0
    # ou 0.0. Usamos nosso DB como source-of-truth.
    if not closed:
        local = await _local_resolve_if_extreme(
            conn, position_id, condition_id, outcome, size, avg_entry,
        )
        if local is not None:
            return local  # fechou por inferência local
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
        "unrealized_pnl=0, close_reason='resolved' WHERE id=?",
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
