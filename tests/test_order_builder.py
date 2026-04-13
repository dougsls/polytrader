"""Diretiva 4 — construção de ordens com flag neg_risk e quantização."""
from decimal import Decimal

import pytest

from src.api.order_builder import MarketSpec, build_order


def _spec(neg_risk: bool = False) -> MarketSpec:
    return MarketSpec(
        condition_id="0xcond",
        token_id="tok123",
        tick_size=Decimal("0.01"),
        size_step=Decimal("1"),
        neg_risk=neg_risk,
    )


def test_build_order_quantizes_and_propagates_neg_risk():
    draft = build_order(_spec(neg_risk=True), "BUY", 0.537, 7.6)
    assert draft.price == Decimal("0.54")
    assert draft.size == Decimal("8")
    assert draft.neg_risk is True
    assert draft.side == "BUY"


def test_build_order_regular_market():
    draft = build_order(_spec(neg_risk=False), "SELL", "0.123", "3")
    assert draft.neg_risk is False
    assert draft.price == Decimal("0.12")


def test_build_order_rejects_zero_size_after_quantize():
    # size=0.3 com step=1 → quantiza pra 0 → erro
    with pytest.raises(ValueError):
        build_order(_spec(), "BUY", 0.5, 0.3)
