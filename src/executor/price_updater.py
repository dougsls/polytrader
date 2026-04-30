"""Atualiza current_price + unrealized_pnl das bot_positions abertas.

Sem isso o dashboard mostra PnL latente $0 sempre (current_price=entry_price).

⚠️ HFT INFRA — refactor 2026-04:
    Antes: loop sequencial fazia 100 GET /price seguidos a ~100ms cada
    = 10s bloqueado. Pior: 100 UPDATE individuais com locks SQLite,
    contendendo contra apply_fill do hot path durante a janela inteira.
    Agora: asyncio.Semaphore(10) + asyncio.gather paralelizam fetches
    (10× speedup → ~1s). UPDATEs viram um único executemany + 1 commit
    (1 write lock contra 100). WAL handle write rápido; readers
    permanecem fluidos.

Task loop a cada PRICE_UPDATE_INTERVAL segundos:
  1. SELECT bot_positions WHERE is_open=1 → token_ids
  2. Para cada token, GET /price?token_id=X&side=SELL EM PARALELO
     (cap 10 concurrent — Polymarket comporta ~50 req/s mas reservamos
     folga pro hot path do CLOB)
  3. executemany UPDATE bot_positions SET current_price, unrealized_pnl
     unrealized_pnl = (current_price - avg_entry_price) * size
"""
from __future__ import annotations

import asyncio
from typing import Any

import aiosqlite

from src.api.clob_client import CLOBClient
from src.core.logger import get_logger

log = get_logger(__name__)

PRICE_UPDATE_INTERVAL_SECONDS = 30
# Concurrency cap pro fetch /price em rajada. Polymarket data-api +
# CLOB juntos comportam ~50 req/s; 10 nos deixa folga 5x pro hot path.
PRICE_FETCH_CONCURRENCY = 10


async def _fetch_one_price(
    clob: CLOBClient,
    sem: asyncio.Semaphore,
    pid: int,
    token_id: str,
    size: float,
    entry: float,
) -> tuple[int, float, float] | None:
    """Fetch único protegido por Semaphore. Retorna (pid, current, unrealized)
    ou None em falha. Erros logados internamente — não propagam pro gather."""
    async with sem:
        try:
            current = await clob.price(token_id, side="SELL")
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "price_fetch_failed",
                token_id=token_id[:12], err=repr(exc)[:80],
            )
            return None
    unrealized = (current - entry) * size
    return (pid, current, unrealized)


async def update_prices_once(
    conn: aiosqlite.Connection,
    clob: CLOBClient,
) -> int:
    """Atualiza todas posições abertas em paralelo. Retorna quantas
    foram atualizadas."""
    async with conn.execute(
        "SELECT id, token_id, size, avg_entry_price "
        "FROM bot_positions WHERE is_open=1"
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        return 0

    # === Fase 1: fetch paralelo de preços ===
    sem = asyncio.Semaphore(PRICE_FETCH_CONCURRENCY)
    tasks = [
        asyncio.create_task(_fetch_one_price(
            clob, sem, row[0], row[1], float(row[2]), float(row[3]),
        ))
        for row in rows
    ]
    results: list[Any] = await asyncio.gather(*tasks, return_exceptions=True)

    # === Fase 2: filtra sucessos e prepara batch ===
    # Ordem dos params do executemany: (current, unrealized, pid)
    # — bate com o SQL `SET current_price=?, unrealized_pnl=? WHERE id=?`
    batch_params: list[tuple[float, float, int]] = []
    for r in results:
        if isinstance(r, BaseException) or r is None:
            continue
        pid, current, unrealized = r
        batch_params.append((current, unrealized, pid))

    if not batch_params:
        return 0

    # === Fase 3: 1 executemany + 1 commit ===
    # Reduz N writes locks pra 1. WAL handle eficiente; copy_engine
    # apply_fill não compete por locks durante a janela.
    await conn.executemany(
        "UPDATE bot_positions SET current_price=?, unrealized_pnl=?, "
        "updated_at=datetime('now') WHERE id=?",
        batch_params,
    )
    await conn.commit()
    return len(batch_params)


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
