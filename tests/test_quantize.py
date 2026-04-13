"""Diretiva 1 — quantização de preço/size."""
from decimal import Decimal

import pytest

from src.core.quantize import quantize_price, quantize_size


def test_quantize_price_rounds_to_tick():
    # tick 0.01 → 0.537 vira 0.54
    assert quantize_price(0.537, 0.01) == Decimal("0.54")


def test_quantize_price_handles_float_ulp():
    # 0.1 + 0.2 = 0.30000000000000004 em float; Decimal resolve.
    price = 0.1 + 0.2
    assert quantize_price(price, 0.01) == Decimal("0.30")


def test_quantize_price_tick_0_001():
    assert quantize_price("0.12345", "0.001") == Decimal("0.123")


def test_quantize_size_step_1():
    assert quantize_size("7.6", "1") == Decimal("8")


def test_quantize_rejects_non_positive_tick():
    with pytest.raises(ValueError):
        quantize_price(0.5, 0)
    with pytest.raises(ValueError):
        quantize_size(1.0, -0.01)
