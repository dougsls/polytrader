"""Testes da função pura compute_optimistic_ref_price (HFT mode)."""
from __future__ import annotations

import pytest

from src.executor.slippage import compute_optimistic_ref_price


def test_optimistic_buy_embeds_tolerance_above_whale():
    # Whale comprou a 0.50, tolerância 3% → topo aceitável é 0.515.
    ref = compute_optimistic_ref_price(side="BUY", whale_price=0.50, tolerance_pct=0.03)
    assert ref == pytest.approx(0.515, rel=1e-9)


def test_optimistic_sell_embeds_tolerance_below_whale():
    # Whale vendeu a 0.50, tolerância 3% → piso aceitável é 0.485.
    ref = compute_optimistic_ref_price(side="SELL", whale_price=0.50, tolerance_pct=0.03)
    assert ref == pytest.approx(0.485, rel=1e-9)


def test_optimistic_zero_tolerance_returns_whale_price():
    # Sem tolerância, ref_price = whale_price exato (FOK on best limit).
    assert compute_optimistic_ref_price(
        side="BUY", whale_price=0.42, tolerance_pct=0.0,
    ) == 0.42
    assert compute_optimistic_ref_price(
        side="SELL", whale_price=0.42, tolerance_pct=0.0,
    ) == 0.42


def test_optimistic_rejects_invalid_whale_price():
    with pytest.raises(ValueError):
        compute_optimistic_ref_price(side="BUY", whale_price=0.0, tolerance_pct=0.03)
    with pytest.raises(ValueError):
        compute_optimistic_ref_price(side="BUY", whale_price=-0.5, tolerance_pct=0.03)


def test_optimistic_pure_function_no_io():
    # Função é síncrona — não exige clob_client. Garante zero round-trip.
    # Repete 1000× — deve ser sub-ms.
    import time
    start = time.perf_counter()
    for _ in range(1000):
        compute_optimistic_ref_price(side="BUY", whale_price=0.50, tolerance_pct=0.03)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.05  # 1000 calls em <50ms
