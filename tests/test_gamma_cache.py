"""Fase 2 — Gamma client persiste tick_size + neg_risk no cache."""
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.api.gamma_client import GammaAPIClient
from src.core.database import init_database


async def _setup_db(tmp_path: Path) -> Path:
    db = tmp_path / "test.db"
    await init_database(db)
    return db


async def test_cache_writes_tick_and_neg_risk(tmp_path: Path):
    db_path = await _setup_db(tmp_path)
    client = GammaAPIClient(db_path=db_path, ttl_seconds=300)
    market = {
        "condition_id": "0xabc",
        "question": "Will X happen?",
        "slug": "x-happen",
        "end_date_iso": "2026-05-01T00:00:00Z",
        "minimum_tick_size": 0.001,
        "neg_risk": True,
        "active": True,
        "closed": False,
        "resolved": False,
        "tokens": [{"token_id": "t1"}, {"token_id": "t2"}],
    }
    with patch.object(client, "_get", new=AsyncMock(return_value=[market])):
        result = await client.get_market("0xabc")
    assert result["neg_risk"] is True

    cached = await client._read_cache("0xabc")
    assert cached is not None
    assert cached["tick_size"] == 0.001
    assert cached["neg_risk"] is True


async def test_cache_hit_skips_http(tmp_path: Path):
    db_path = await _setup_db(tmp_path)
    client = GammaAPIClient(db_path=db_path, ttl_seconds=300)
    market = {
        "condition_id": "0xdef",
        "question": "q",
        "slug": "s",
        "end_date_iso": "2026-05-01T00:00:00Z",
        "minimum_tick_size": 0.01,
        "neg_risk": False,
        "active": True,
        "closed": False,
        "resolved": False,
        "tokens": [],
    }
    mock_get = AsyncMock(return_value=[market])
    with patch.object(client, "_get", new=mock_get):
        await client.get_market("0xdef")
        await client.get_market("0xdef")
        await client.get_market("0xdef")
    assert mock_get.call_count == 1
