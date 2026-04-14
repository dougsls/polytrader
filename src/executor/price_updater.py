"""Atualiza current_price + unrealized_pnl das bot_positions abertas.

Sem isso o dashboard mostra PnL latente $0 sempre (current_price=entry_price).

Task loop a cada PRICE_UPDATE_INTERVAL segundos:
  1. SELECT bot_positions WHERE is_open=1 → token_ids
  2. Para cada token, GET /price?token_id=X&side=SELL do CLOB (preço que
     a gente receberia se vendesse AGORA)
  3. UPDATE bot_positions SET current_price, unrealized_pnl
     unrealized_pnl = (current_price - avg_entry_price) * size
"""
from __future__ import annotations

import asyncio

import aiosqlite

from src.api.clob_client import CLOBClient
from src.core.logger import get_logger

log = get_logger(__name__)

PRICE_UPDATE_INTERVAL_SECONDS = 30


async def update_prices_once(
    conn: aiosqlite.Connection,
    clob: CLOBClient,
) -> int:
    """Atualiza todas posições abertas. Retorna quantas foram atualizadas."""
    async with conn.execute(
        "SELECT id, token_id, size, avg_entry_price "
        "FROM bot_positions WHERE is_open=1"
    ) as cur:
        rows = await cur.fetchall()

    updated = 0
    for row in rows:
        pid, token_id, size, entry = row[0], row[1], float(row[2]), float(row[3])
        try:
            # SELL side = preço que receberíamos ao fechar a posição agora.
            current = await clob.price(token_id, side="SELL")
        except Exception as exc:  # noqa: BLE001
            log.warning("price_fetch_failed",
                        token_id=token_id[:12], err=repr(exc))
            continue
        unrealized = (current - entry) * size
        await conn.execute(
            "UPDATE bot_positions SET current_price=?, unrealized_pnl=?, "
            "updated_at=datetime('now') WHERE id=?",
            (current, unrealized, pid),
        )
        updated += 1
    if updated:
        await conn.commit()
    return updated


async def price_update_loop(
    *,
    shutdown: asyncio.Event,
    conn: aiosqlite.Connection,
    clob: CLOBClient,
) -> None:
    while not shutdown.is_set():
        try:
            n = await update_prices_once(conn, clob)
            if n:
                log.info("prices_updated", count=n)
        except Exception as exc:  # noqa: BLE001
            log.error("price_update_loop_crash", err=repr(exc))
        try:
            await asyncio.wait_for(
                shutdown.wait(), timeout=PRICE_UPDATE_INTERVAL_SECONDS,
            )
        except asyncio.TimeoutError:
            pass
