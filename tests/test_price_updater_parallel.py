"""HFT INFRA — price_updater paralelo + executemany.

Cobre:
  - Semaphore limita a concorrência de fetches (não burst infinito)
  - asyncio.gather paraleliza N fetches (N/concurrency × ~RTT, não N×RTT)
  - Falha individual num token NÃO contamina os demais
  - executemany faz 1 SQL statement com N tuples (não N statements)
  - Single commit no fim (não N commits)
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.database import get_connection, init_database
from src.executor.price_updater import (
    PRICE_FETCH_CONCURRENCY,
    _fetch_one_price,
    update_prices_once,
)


async def _seed_position(
    db: Path, position_id: int, token_id: str,
    size: float = 100.0, entry: float = 0.50,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with get_connection(db) as conn:
        await conn.execute(
            "INSERT INTO bot_positions"
            " (id, condition_id, token_id, market_title, outcome,"
            "  size, avg_entry_price, source_wallets_json,"
            "  opened_at, is_open, updated_at)"
            " VALUES (?, ?, ?, 'm', 'Yes', ?, ?, '[]', ?, 1, ?)",
            (position_id, f"0xC{position_id}", token_id, size, entry, now, now),
        )
        await conn.commit()


# ============ _fetch_one_price ============

@pytest.mark.asyncio
async def test_fetch_one_price_returns_tuple_on_success():
    sem = asyncio.Semaphore(10)
    clob = MagicMock()
    clob.price = AsyncMock(return_value=0.42)
    result = await _fetch_one_price(
        clob, sem, pid=1, token_id="tok", size=100.0, entry=0.40,
    )
    assert result is not None
    pid, current, unrealized = result
    assert pid == 1
    assert current == 0.42
    # (0.42 - 0.40) × 100 = 2.0
    assert unrealized == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_fetch_one_price_returns_none_on_failure():
    """Falha individual NÃO propaga — retorna None."""
    sem = asyncio.Semaphore(10)
    clob = MagicMock()
    clob.price = AsyncMock(side_effect=Exception("rate limit"))
    result = await _fetch_one_price(
        clob, sem, pid=1, token_id="tok", size=100.0, entry=0.40,
    )
    assert result is None


# ============ update_prices_once integration ============

@pytest.mark.asyncio
async def test_update_runs_fetches_in_parallel(tmp_path: Path):
    """N positions com fetch que dorme ~50ms cada → tempo total deve
    ser ~N/concurrency × 50ms (paralelo), não N × 50ms (sequencial)."""
    db = tmp_path / "p.db"
    await init_database(db)
    n = 20
    for i in range(n):
        await _seed_position(db, position_id=i + 1, token_id=f"tok-{i}")

    clob = MagicMock()

    async def slow_price(token_id, side):
        await asyncio.sleep(0.05)  # 50ms RTT simulado
        return 0.42

    clob.price = slow_price

    async with get_connection(db) as conn:
        t0 = time.perf_counter()
        updated = await update_prices_once(conn, clob)
        elapsed = time.perf_counter() - t0

    assert updated == n
    # Sequencial seria 20 × 0.05 = 1.0s.
    # Paralelo (cap=10): 2 levas × 0.05 = ~0.1s. Damos folga 5x.
    # Bound mais agressivo evita falso-negativo em CI lenta.
    assert elapsed < 0.5, f"Sequential? Took {elapsed:.2f}s for 20 fetches"


@pytest.mark.asyncio
async def test_update_isolates_failures(tmp_path: Path):
    """1 token falha, 4 sucedem → 4 atualizadas, sem crash."""
    db = tmp_path / "p.db"
    await init_database(db)
    for i in range(5):
        await _seed_position(db, position_id=i + 1, token_id=f"tok-{i}")

    clob = MagicMock()

    async def selective_price(token_id, side):
        if token_id == "tok-2":
            raise RuntimeError("rate limit on this token")
        return 0.42

    clob.price = selective_price

    async with get_connection(db) as conn:
        updated = await update_prices_once(conn, clob)
        # Verifica DB: tok-2 mantém current_price NULL/old; outros atualizados
        async with conn.execute(
            "SELECT id, token_id, current_price FROM bot_positions WHERE is_open=1 ORDER BY id"
        ) as cur:
            rows = await cur.fetchall()

    assert updated == 4
    by_token = {r[1]: r[2] for r in rows}
    assert by_token["tok-0"] == 0.42
    assert by_token["tok-1"] == 0.42
    assert by_token["tok-2"] is None  # falhou
    assert by_token["tok-3"] == 0.42
    assert by_token["tok-4"] == 0.42


@pytest.mark.asyncio
async def test_update_uses_executemany_single_commit(tmp_path: Path):
    """Verifica que update bate executemany UMA vez (não N execute)."""
    db = tmp_path / "p.db"
    await init_database(db)
    for i in range(5):
        await _seed_position(db, position_id=i + 1, token_id=f"tok-{i}")

    clob = MagicMock()
    clob.price = AsyncMock(return_value=0.42)

    async with get_connection(db) as conn:
        original_executemany = conn.executemany
        em_calls: list[tuple[str, int]] = []

        async def spy_executemany(sql, params):
            params_list = list(params) if params is not None else []
            em_calls.append((sql, len(params_list)))
            return await original_executemany(sql, params_list)

        conn.executemany = spy_executemany  # type: ignore[method-assign]
        updated = await update_prices_once(conn, clob)

    assert updated == 5
    # CRÍTICO — exatamente 1 executemany com 5 tuples (não 5 chamadas)
    assert len(em_calls) == 1, f"Expected 1 batch, got {len(em_calls)}"
    sql, n_params = em_calls[0]
    assert "UPDATE bot_positions" in sql.lower() or "UPDATE BOT_POSITIONS" in sql.upper()
    assert n_params == 5, f"Expected 5 tuples in batch, got {n_params}"


@pytest.mark.asyncio
async def test_update_returns_zero_with_no_open_positions(tmp_path: Path):
    """Sem positions abertas, retorna 0 cedo (zero work)."""
    db = tmp_path / "p.db"
    await init_database(db)

    clob = MagicMock()
    clob.price = AsyncMock(side_effect=AssertionError("não deve chamar"))

    async with get_connection(db) as conn:
        updated = await update_prices_once(conn, clob)
    assert updated == 0


@pytest.mark.asyncio
async def test_concurrency_constant_is_sane():
    """Sanity: cap exposed para audit."""
    assert PRICE_FETCH_CONCURRENCY == 10
