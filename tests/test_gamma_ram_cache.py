"""HFT — Item 1: GammaAPIClient RAM cache (3-tier).

Cobre:
  - RAM hit em sub-µs (sem fetch)
  - LRU eviction quando cap atingido
  - TTL drop ao expirar
  - Promotion: SQLite hit promove pra RAM
  - Métricas hits/misses
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.api.gamma_client import RAM_CACHE_MAX_SIZE, GammaAPIClient
from src.core.database import init_database


@pytest.mark.asyncio
async def test_ram_cache_hit_skips_io(tmp_path: Path):
    """Após primeira write, leitura volta da RAM sem tocar SQLite ou REST."""
    db = tmp_path / "g.db"
    await init_database(db)
    g = GammaAPIClient(db_path=db, ttl_seconds=60)

    market = {
        "conditionId": "0xCID",
        "question": "Will X?",
        "tokens": [{"token_id": "t1", "outcome": "Yes"}],
        "tick_size": 0.01,
    }
    # Mock REST: deve ser chamado UMA vez (cold path).
    with patch.object(
        g, "_get", AsyncMock(return_value=[market])
    ) as mock_get:
        m1 = await g.get_market("0xCID")
        m2 = await g.get_market("0xCID")  # RAM hit
        m3 = await g.get_market("0xCID")  # RAM hit
    assert m1 == m2 == m3
    assert mock_get.call_count == 1  # apenas 1 fetch REST
    stats = g.ram_stats
    assert stats["hits"] >= 2
    assert stats["size"] == 1


@pytest.mark.asyncio
async def test_ram_cache_evicts_on_overflow(tmp_path: Path):
    """Quando cap atingido, oldest entry é despejado (LRU)."""
    db = tmp_path / "g.db"
    await init_database(db)
    g = GammaAPIClient(db_path=db, ttl_seconds=60)

    # Insere RAM_CACHE_MAX_SIZE + 5 entries diretamente.
    for i in range(RAM_CACHE_MAX_SIZE + 5):
        g._ram_set(f"cid-{i}", {"conditionId": f"cid-{i}"})
    assert len(g._ram_cache) == RAM_CACHE_MAX_SIZE
    # As 5 primeiras saíram (LRU).
    assert "cid-0" not in g._ram_cache
    assert "cid-4" not in g._ram_cache
    assert "cid-5" in g._ram_cache
    assert f"cid-{RAM_CACHE_MAX_SIZE + 4}" in g._ram_cache


@pytest.mark.asyncio
async def test_ram_cache_ttl_expires(tmp_path: Path):
    """Entry com idade > ttl é dropped no _ram_get."""
    db = tmp_path / "g.db"
    await init_database(db)
    g = GammaAPIClient(db_path=db, ttl_seconds=60)

    # Injeta com timestamp falso (mais velho que TTL).
    g._ram_cache["expired"] = ({"conditionId": "expired"}, time.monotonic() - 1000)
    assert g._ram_get("expired") is None
    # Foi despejado.
    assert "expired" not in g._ram_cache


@pytest.mark.asyncio
async def test_ram_cache_lru_move_to_end_on_hit(tmp_path: Path):
    """Hit em entrada antiga move pro fim (LRU re-promotion)."""
    db = tmp_path / "g.db"
    await init_database(db)
    g = GammaAPIClient(db_path=db, ttl_seconds=60)

    g._ram_set("a", {"conditionId": "a"})
    g._ram_set("b", {"conditionId": "b"})
    g._ram_set("c", {"conditionId": "c"})
    # Order: a, b, c
    assert list(g._ram_cache.keys()) == ["a", "b", "c"]
    g._ram_get("a")  # hit
    # Order agora: b, c, a
    assert list(g._ram_cache.keys()) == ["b", "c", "a"]


@pytest.mark.asyncio
async def test_ram_promotion_from_sqlite(tmp_path: Path):
    """SQLite hit deve promover pra RAM — próximo lookup é L1."""
    db = tmp_path / "g.db"
    await init_database(db)
    g = GammaAPIClient(db_path=db, ttl_seconds=60)

    # Bypass do REST: escreve diretamente via _write_cache (que mira RAM+DB).
    market = {
        "conditionId": "promo-cid",
        "question": "X?",
        "tokens": [{"token_id": "t", "outcome": "Yes"}],
        "tick_size": 0.01,
    }
    await g._write_cache("promo-cid", market)
    # Limpa RAM pra simular reboot do processo (SQLite ainda tem).
    g._ram_cache.clear()
    g._ram_hits = g._ram_misses = 0

    with patch.object(g, "_get", AsyncMock(side_effect=AssertionError("REST não devia ser chamado"))):
        result = await g.get_market("promo-cid")

    # _read_cache retorna o shape normalizado (`condition_id`, não `conditionId`).
    assert result["condition_id"] == "promo-cid"
    # Após get, RAM deve ter a entrada promovida.
    assert "promo-cid" in g._ram_cache
    # Próxima chamada é L1 hit puro.
    with patch.object(g, "_get", AsyncMock(side_effect=AssertionError("nem REST nem SQLite"))):
        with patch.object(g, "_read_cache", AsyncMock(side_effect=AssertionError("SQLite tampouco"))):
            await g.get_market("promo-cid")
