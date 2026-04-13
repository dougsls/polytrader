"""Regra 1 — Anti-Slippage Anchoring: prova de trava."""
from unittest.mock import AsyncMock

import pytest

from src.core.exceptions import SlippageExceededError
from src.executor.slippage import check_slippage_or_abort


class _FakeCLOB:
    def __init__(self, book: dict) -> None:
        self.book = AsyncMock(return_value=book)


async def test_buy_within_tolerance_returns_price():
    clob = _FakeCLOB({"asks": [{"price": "0.51"}, {"price": "0.52"}], "bids": []})
    price = await check_slippage_or_abort(
        clob=clob, token_id="t1", side="BUY",
        whale_price=0.50, tolerance_pct=0.03,  # tolera até 0.515
    )
    assert price == 0.51


async def test_buy_beyond_tolerance_raises():
    # whale em 0.50, tolerance 3% → limite 0.515. Ask em 0.55 → ABORT.
    clob = _FakeCLOB({"asks": [{"price": "0.55"}], "bids": []})
    with pytest.raises(SlippageExceededError) as exc:
        await check_slippage_or_abort(
            clob=clob, token_id="t1", side="BUY",
            whale_price=0.50, tolerance_pct=0.03,
        )
    assert exc.value.actual > 0.03


async def test_sell_within_tolerance_returns_price():
    # whale em 0.60, tolerance 3% → bid mínimo aceito 0.582.
    clob = _FakeCLOB({"asks": [], "bids": [{"price": "0.585"}]})
    price = await check_slippage_or_abort(
        clob=clob, token_id="t1", side="SELL",
        whale_price=0.60, tolerance_pct=0.03,
    )
    assert price == 0.585


async def test_sell_below_tolerance_raises():
    # whale em 0.60 → bid mínimo aceito 0.582. Bid 0.55 → ABORT.
    clob = _FakeCLOB({"asks": [], "bids": [{"price": "0.55"}]})
    with pytest.raises(SlippageExceededError):
        await check_slippage_or_abort(
            clob=clob, token_id="t1", side="SELL",
            whale_price=0.60, tolerance_pct=0.03,
        )


async def test_whale_price_zero_raises_value_error():
    clob = _FakeCLOB({"asks": [{"price": "0.5"}], "bids": []})
    with pytest.raises(ValueError):
        await check_slippage_or_abort(
            clob=clob, token_id="t1", side="BUY",
            whale_price=0.0, tolerance_pct=0.03,
        )
