"""MEV-aware gas oracle para Polygon.

Em volatilidade (que é exatamente quando arbitragem aparece), priority
fee mediano sobe rápido — 30 gwei hardcoded fica pra trás, tx cai do
bloco ou sofre MEV-sandwich. Esta camada estima `maxPriorityFeePerGas`
em tempo real via cascata de fontes:

    L1: Polygon Gas Station v2 (oficial, atualizada bloco-a-bloco)
        → `fast.maxPriorityFee` em gwei
    L2: eth_feeHistory percentil 99 dos últimos 20 blocos
        → fallback 100% on-chain, sem dependência externa
    L3: Hardcoded 30 gwei (default histórico)
        → último recurso

Cache em RAM com TTL 5s — chamado N×/segundo em rajada de arb não pode
hammerar a gas station. Polygon block time ~2.2s; 5s = ~2 blocos de
defasagem máxima (aceitável vs custo de rede).
"""
from __future__ import annotations

import time
from typing import Any

import orjson

from src.api.http import get_http_client
from src.core.logger import get_logger

log = get_logger(__name__)

DEFAULT_GAS_STATION_URL = "https://gasstation.polygon.technology/v2"
GAS_CACHE_TTL_SECONDS = 5.0
# Default histórico — preserva comportamento legado quando ambas fontes
# falham. 30 gwei é o piso "minimum gas" da rede em condições normais.
HARDCODED_FALLBACK_GWEI = 30.0
# Cap defensivo: nunca mandar mais que 500 gwei mesmo com gas station
# devolvendo absurdo (proteção contra spike outlier).
PRIORITY_FEE_CAP_GWEI = 500.0
# Para fee_history fallback: pegar o top 99 percentile dos últimos N blocks.
FEE_HISTORY_BLOCKS = 20
FEE_HISTORY_PERCENTILE = 99


class GasOracle:
    """Cacheia priority fee dinâmico. Singleton por CTFClient."""

    def __init__(
        self,
        gas_station_url: str = DEFAULT_GAS_STATION_URL,
        ttl_seconds: float = GAS_CACHE_TTL_SECONDS,
    ) -> None:
        self._url = gas_station_url
        self._ttl = ttl_seconds
        # (priority_fee_wei, fetched_at_monotonic)
        self._cache: tuple[int, float] | None = None

    async def get_priority_fee_wei(self, w3: Any | None = None) -> int:
        """Retorna `maxPriorityFeePerGas` em wei.

        Args:
            w3: instância AsyncWeb3 opcional. Se fornecida, usa como
                fallback quando gas station falha (eth_feeHistory).
        """
        # Cache hit dentro de TTL? — sub-µs.
        if self._cache is not None:
            fee, fetched = self._cache
            if (time.monotonic() - fetched) < self._ttl:
                return fee

        # L1 — Polygon Gas Station v2.
        fee_gwei = await self._try_gas_station()
        source = "gas_station"

        # L2 — eth_feeHistory p99.
        if fee_gwei is None and w3 is not None:
            fee_gwei = await self._try_fee_history(w3)
            source = "fee_history_p99"

        # L3 — hardcoded fallback.
        if fee_gwei is None:
            fee_gwei = HARDCODED_FALLBACK_GWEI
            source = "hardcoded"

        # Cap defensivo.
        if fee_gwei > PRIORITY_FEE_CAP_GWEI:
            log.warning(
                "gas_oracle_capped",
                requested_gwei=fee_gwei, cap_gwei=PRIORITY_FEE_CAP_GWEI,
            )
            fee_gwei = PRIORITY_FEE_CAP_GWEI

        fee_wei = int(fee_gwei * 1e9)
        self._cache = (fee_wei, time.monotonic())
        log.info("gas_oracle_priority_fee", gwei=fee_gwei, source=source)
        return fee_wei

    async def _try_gas_station(self) -> float | None:
        """Polygon Gas Station v2 → fast.maxPriorityFee em gwei.

        Schema esperado:
            {"safeLow":  {"maxPriorityFee": 30.0, "maxFee": 30.5},
             "standard": {"maxPriorityFee": 30.5, "maxFee": 31.0},
             "fast":     {"maxPriorityFee": 31.0, "maxFee": 31.5},
             "estimatedBaseFee": 0.0, ...}
        """
        try:
            client = await get_http_client()
            resp = await client.get(self._url, timeout=3.0)
            if resp.status_code != 200:
                log.debug("gas_station_non_200", status=resp.status_code)
                return None
            data: dict[str, Any] = orjson.loads(resp.content)
            fast = data.get("fast") or {}
            fee = fast.get("maxPriorityFee")
            if fee is None:
                return None
            return float(fee)
        except Exception as exc:  # noqa: BLE001 — fallback é silent, qualquer
            # erro (rede, parse, schema) cai pro próximo nível da cascata.
            log.debug("gas_station_failed", err=repr(exc))
            return None

    async def _try_fee_history(self, w3: Any) -> float | None:
        """eth_feeHistory percentil 99 dos últimos N blocos.

        Retorna gwei. None se RPC falha ou retorna shape inesperado.
        """
        try:
            hist = await w3.eth.fee_history(
                FEE_HISTORY_BLOCKS, "latest",
                reward_percentiles=[FEE_HISTORY_PERCENTILE],
            )
            rewards = hist.get("reward") if isinstance(hist, dict) else getattr(hist, "reward", None)
            if not rewards:
                return None
            # rewards = list[list[int_wei]] — cada bloco × N percentis.
            # Pegamos o p99 de cada bloco (índice 0 quando passamos só [99]).
            p99_per_block = [r[0] for r in rewards if r and len(r) > 0]
            if not p99_per_block:
                return None
            # Mediana dos p99s — robusto contra blocos vazios (que retornam 0).
            p99_per_block.sort()
            median_p99_wei = p99_per_block[len(p99_per_block) // 2]
            if median_p99_wei <= 0:
                return None
            return median_p99_wei / 1e9  # wei → gwei
        except Exception as exc:  # noqa: BLE001 — fallback é silent
            log.debug("fee_history_failed", err=repr(exc))
            return None
