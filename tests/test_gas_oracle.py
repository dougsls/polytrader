"""MEV — GasOracle: cascade de fontes (gas station → fee_history → fallback)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import orjson
import pytest

from src.arbitrage.gas_oracle import (
    GAS_CACHE_TTL_SECONDS,
    HARDCODED_FALLBACK_GWEI,
    PRIORITY_FEE_CAP_GWEI,
    GasOracle,
)


def _mk_resp(status: int, body: dict | None = None):
    r = MagicMock()
    r.status_code = status
    r.content = orjson.dumps(body or {})
    return r


@pytest.mark.asyncio
async def test_gas_station_primary_source():
    """L1: Polygon Gas Station retorna fast.maxPriorityFee em gwei."""
    oracle = GasOracle()
    body = {
        "safeLow": {"maxPriorityFee": 30.0, "maxFee": 30.5},
        "standard": {"maxPriorityFee": 35.0, "maxFee": 35.5},
        "fast": {"maxPriorityFee": 75.5, "maxFee": 80.0},
    }
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=_mk_resp(200, body))

    with patch("src.arbitrage.gas_oracle.get_http_client",
               AsyncMock(return_value=mock_client)):
        fee_wei = await oracle.get_priority_fee_wei()

    assert fee_wei == int(75.5 * 1e9)


@pytest.mark.asyncio
async def test_falls_back_to_fee_history_when_gas_station_503():
    """L2: gas station fora do ar → eth_feeHistory p99."""
    oracle = GasOracle()
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=_mk_resp(503))

    # Mock AsyncWeb3 — fee_history retorna p99 em wei por bloco
    w3 = MagicMock()
    w3.eth.fee_history = AsyncMock(return_value={
        "reward": [
            [int(40e9)], [int(50e9)], [int(60e9)],  # 3 blocos com p99 em wei
            [int(45e9)], [int(55e9)],
        ]
    })

    with patch("src.arbitrage.gas_oracle.get_http_client",
               AsyncMock(return_value=mock_client)):
        fee_wei = await oracle.get_priority_fee_wei(w3=w3)

    # Mediana dos p99s = 50e9 wei (= 50 gwei)
    assert fee_wei == int(50e9)


@pytest.mark.asyncio
async def test_falls_back_to_hardcoded_when_all_sources_fail():
    """L3: gas station + fee_history falham → fallback 30 gwei."""
    oracle = GasOracle()
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=Exception("network"))

    w3 = MagicMock()
    w3.eth.fee_history = AsyncMock(side_effect=Exception("rpc dead"))

    with patch("src.arbitrage.gas_oracle.get_http_client",
               AsyncMock(return_value=mock_client)):
        fee_wei = await oracle.get_priority_fee_wei(w3=w3)

    assert fee_wei == int(HARDCODED_FALLBACK_GWEI * 1e9)


@pytest.mark.asyncio
async def test_cache_avoids_repeated_fetches_within_ttl():
    """Cache TTL 5s — second call no-op no I/O."""
    oracle = GasOracle()
    body = {"fast": {"maxPriorityFee": 42.0, "maxFee": 50.0}}
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=_mk_resp(200, body))

    with patch("src.arbitrage.gas_oracle.get_http_client",
               AsyncMock(return_value=mock_client)):
        fee1 = await oracle.get_priority_fee_wei()
        fee2 = await oracle.get_priority_fee_wei()  # cache hit
        fee3 = await oracle.get_priority_fee_wei()  # cache hit

    assert fee1 == fee2 == fee3 == int(42 * 1e9)
    # Apenas 1 chamada HTTP (cache) — economia em rajada de arb.
    assert mock_client.get.call_count == 1


@pytest.mark.asyncio
async def test_caps_outlier_priority_fees():
    """Cap defensivo: gas station devolvendo 9999 gwei → cap em 500."""
    oracle = GasOracle()
    body = {"fast": {"maxPriorityFee": 9999.0, "maxFee": 10000.0}}
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=_mk_resp(200, body))

    with patch("src.arbitrage.gas_oracle.get_http_client",
               AsyncMock(return_value=mock_client)):
        fee_wei = await oracle.get_priority_fee_wei()

    assert fee_wei == int(PRIORITY_FEE_CAP_GWEI * 1e9)


@pytest.mark.asyncio
async def test_fee_history_handles_empty_rewards():
    """fee_history com p99 zerado em todos blocos → falha pra fallback."""
    oracle = GasOracle()
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=_mk_resp(503))

    w3 = MagicMock()
    w3.eth.fee_history = AsyncMock(return_value={"reward": [[0], [0], [0]]})

    with patch("src.arbitrage.gas_oracle.get_http_client",
               AsyncMock(return_value=mock_client)):
        fee_wei = await oracle.get_priority_fee_wei(w3=w3)

    # 0 não é fee válido → cai pro hardcoded.
    assert fee_wei == int(HARDCODED_FALLBACK_GWEI * 1e9)


@pytest.mark.asyncio
async def test_fee_history_object_attribute_access():
    """Algumas versões web3.py retornam dataclass-like com .reward attr."""
    oracle = GasOracle()
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=_mk_resp(503))

    fake = MagicMock()
    fake.reward = [[int(50e9)], [int(60e9)]]
    # AttrDict-like: getattr funciona, .get pode não estar disponível.
    # GasOracle deve usar getattr() como fallback.

    w3 = MagicMock()
    w3.eth.fee_history = AsyncMock(return_value=fake)

    with patch("src.arbitrage.gas_oracle.get_http_client",
               AsyncMock(return_value=mock_client)):
        fee_wei = await oracle.get_priority_fee_wei(w3=w3)

    # Mediana de [50e9, 60e9] = 60e9 (lista ordenada, índice n//2 = 1).
    assert fee_wei == int(60e9)
