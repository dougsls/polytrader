"""Diretiva HFT 4 — Ordens em mercados Negative Risk (multi-outcome).

Mercados com múltiplos desfechos (eleição com 5+ candidatos, campeonato)
usam o exchange neg_risk da Polymarket, que aplica netting de posições
entre os outcomes complementares (Σ preços = 1.0). O SDK oficial
py-clob-client requer que o cliente construa a ordem com o adapter
correto e inclua o header `NEG_RISK` apropriado no postOrder.

Este módulo é um scaffold tipado consumido pelo futuro clob_client. Ele
não executa rede — apenas encapsula a decisão de rota neg_risk vs padrão
a partir do metadado cacheado em `market_metadata_cache.neg_risk`.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from src.core.quantize import quantize_price, quantize_size

Side = Literal["BUY", "SELL"]


@dataclass(frozen=True, slots=True)
class MarketSpec:
    """Metadados quantitativos necessários para montar uma ordem válida.

    Fonte: tabela market_metadata_cache (populada pelo gamma_client).
    """

    condition_id: str
    token_id: str
    tick_size: Decimal
    size_step: Decimal
    neg_risk: bool


@dataclass(frozen=True, slots=True)
class OrderDraft:
    """Ordem pronta para assinatura EIP-712 + postOrder.

    `tick_size` é propagado aqui (além de já estar no MarketSpec) para que
    o clob_client construa `PartialCreateOrderOptions(tick_size=..., neg_risk=...)`
    sem precisar de outro lookup no cache.
    """

    token_id: str
    side: Side
    price: Decimal
    size: Decimal
    neg_risk: bool
    tick_size: Decimal


def build_order(
    spec: MarketSpec,
    side: Side,
    raw_price: float | str | Decimal,
    raw_size: float | str | Decimal,
) -> OrderDraft:
    """Quantiza preço/size e propaga a flag neg_risk.

    Regras aplicadas:
      - Diretiva 1: preço e size alinhados ao tick/step do ativo via Decimal.
      - Diretiva 4: flag neg_risk é copiada ao draft. O clob_client deve
        roteá-la para o exchange correto (neg_risk_adapter do py-clob-client)
        e incluir o header que o SDK exige.
    """
    price = quantize_price(raw_price, spec.tick_size)
    size = quantize_size(raw_size, spec.size_step)
    if price <= 0 or size <= 0:
        raise ValueError(f"ordem inválida após quantização: price={price} size={size}")
    return OrderDraft(
        token_id=spec.token_id,
        side=side,
        price=price,
        size=size,
        neg_risk=spec.neg_risk,
        tick_size=spec.tick_size,
    )
