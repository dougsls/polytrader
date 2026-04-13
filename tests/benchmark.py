"""Benchmarks de hot paths — executados pelo autoresearch loop.

Medimos 2 alvos:
  A) detect_signal (tracker) — caminho completo de qualificação de sinal,
     com gamma mockado e DB quente (sem cache miss).
  B) rtds_parse_and_filter — parsing orjson + filtro Set() do RTDSClient,
     que precisa sustentar ~3k msgs/s em pico.

A métrica agregada é a SOMA das médias de tempo (μs) dos 2 benchmarks.
pytest-benchmark imprime stats; extraímos "mean" das saídas.

Uso:
    uv run pytest tests/benchmark.py -q
    uv run pytest tests/benchmark.py --benchmark-only --benchmark-json=bench.json
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import orjson

from src.api.data_client import DataAPIClient
from src.api.gamma_client import GammaAPIClient
from src.core.config import load_yaml_config
from src.core.database import get_connection, init_database  # noqa: F401
from src.tracker.signal_detector import detect_signal


# -------------------- Fixtures compartilhadas --------------------

async def _seed_db(db: Path) -> None:
    await init_database(db)
    now = datetime.now(timezone.utc).isoformat()
    # Seed bot_position + whale_inventory para o caminho SELL proporcional
    async with get_connection(db) as conn:
        await conn.execute(
            "INSERT INTO bot_positions "
            "(condition_id, token_id, market_title, outcome, size, "
            " avg_entry_price, source_wallets_json, opened_at, is_open, updated_at) "
            "VALUES ('0xCOND','t1','m','Yes',50,0.5,'[\"0xW\"]',?,1,?)",
            (now, now),
        )
        await conn.execute(
            "INSERT INTO whale_inventory (wallet_address, condition_id, token_id, "
            "size, avg_price, last_seen_at) VALUES ('0xW','0xCOND','t1',200,0.5,?)",
            (now,),
        )
        await conn.commit()


def _sync_setup(tmp_path: Path) -> tuple[Path, GammaAPIClient, AsyncMock, dict]:
    db = tmp_path / "bench.db"
    asyncio.run(_seed_db(db))
    end = datetime.now(timezone.utc).timestamp() + 10 * 3600
    end_iso = datetime.fromtimestamp(end, tz=timezone.utc).isoformat()
    gamma = GammaAPIClient(db_path=db)
    gamma.get_market = AsyncMock(return_value={  # type: ignore[method-assign]
        "question": "m", "end_date_iso": end_iso,
        "tokens": [{"token_id": "t1", "outcome": "Yes"}],
        "tick_size": 0.01, "neg_risk": False,
    })
    data = AsyncMock(spec=DataAPIClient)
    trade = {
        "maker": "0xW", "conditionId": "0xCOND", "asset": "t1",
        "side": "SELL", "size": 100.0, "price": 0.5,
        "timestamp": int(datetime.now(timezone.utc).timestamp()),
    }
    return db, gamma, data, trade


# -------------------- Benchmark A: detect_signal --------------------

def test_bench_detect_signal(benchmark, tmp_path: Path):
    """Benchmark representa a chamada real em prod: um asyncio loop
    persistente + conexão SQLite compartilhada pelo tracker."""
    db, gamma, data, trade = _sync_setup(tmp_path)
    cfg = load_yaml_config().tracker

    import aiosqlite
    loop = asyncio.new_event_loop()

    async def _open() -> aiosqlite.Connection:
        return await aiosqlite.connect(db)  # tuple rows (mais leve que Row)

    conn = loop.run_until_complete(_open())

    async def one_call() -> None:
        await detect_signal(
            trade=trade, wallet_score=0.8, cfg=cfg,
            gamma=gamma, data_client=data, db_path=db, conn=conn,
        )

    def run_once() -> None:
        loop.run_until_complete(one_call())

    try:
        benchmark(run_once)
    finally:
        loop.run_until_complete(conn.close())
        loop.close()


# -------------------- Benchmark B: RTDS parse + filter --------------------

def test_bench_rtds_parse_filter(benchmark):
    """Simula o hot path interno do RTDSClient: orjson.loads + Set lookup.

    Payload com 100 trades; filtro deixa passar só 5 (5% hit rate típico
    quando temos ~20 carteiras rastreadas num stream de 3k msgs/s).
    """
    tracked: set[str] = {f"0xW{i:03d}" for i in range(20)}
    payloads = [
        orjson.dumps({
            "maker": f"0xW{i:03d}" if i < 5 else f"0xOTHER{i}",
            "conditionId": "0xC", "asset": "t1", "side": "BUY",
            "size": 10.0, "price": 0.5, "timestamp": 1_700_000_000 + i,
        })
        for i in range(100)
    ]

    def run_batch() -> int:
        hits = 0
        for raw in payloads:
            msg = orjson.loads(raw)
            maker = msg.get("maker") or msg.get("makerAddress")
            if maker in tracked:
                hits += 1
        return hits

    result = benchmark(run_batch)
    assert result == 5
