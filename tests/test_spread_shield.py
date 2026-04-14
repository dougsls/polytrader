"""Escudo de Spread Dinâmico — prova que pools raros são bloqueados."""
from unittest.mock import AsyncMock

import pytest

from src.core.exceptions import SlippageExceededError, SpreadTooWideError
from src.executor.slippage import check_slippage_or_abort


class _FakeCLOB:
    def __init__(self, book: dict) -> None:
        self.book = AsyncMock(return_value=book)


async def test_narrow_spread_passes():
    # ask 0.51, bid 0.50 → spread 0.01 < 0.02
    clob = _FakeCLOB({"asks": [{"price": "0.51"}], "bids": [{"price": "0.50"}]})
    price = await check_slippage_or_abort(
        clob=clob, token_id="t1", side="BUY",
        whale_price=0.50, tolerance_pct=0.03, max_spread=0.02,
    )
    assert price == 0.51


async def test_wide_spread_blocks_buy():
    # ask 0.55, bid 0.50 → spread 0.05 > 0.02 → ABORT
    clob = _FakeCLOB({"asks": [{"price": "0.55"}], "bids": [{"price": "0.50"}]})
    with pytest.raises(SpreadTooWideError) as exc:
        await check_slippage_or_abort(
            clob=clob, token_id="t1", side="BUY",
            whale_price=0.50, tolerance_pct=0.10, max_spread=0.02,
        )
    assert exc.value.spread == pytest.approx(0.05)
    assert exc.value.max_spread == 0.02


async def test_wide_spread_blocks_sell_too():
    clob = _FakeCLOB({"asks": [{"price": "0.60"}], "bids": [{"price": "0.52"}]})
    with pytest.raises(SpreadTooWideError):
        await check_slippage_or_abort(
            clob=clob, token_id="t1", side="SELL",
            whale_price=0.55, tolerance_pct=0.10, max_spread=0.02,
        )


async def test_spread_checked_before_slippage():
    """Se spread E slippage violam, spread aborta primeiro (ordem de trava)."""
    # spread 0.10 (viola 0.02) E ask 0.70 muito acima de whale 0.50 (viola 3%)
    clob = _FakeCLOB({"asks": [{"price": "0.70"}], "bids": [{"price": "0.60"}]})
    with pytest.raises(SpreadTooWideError):
        await check_slippage_or_abort(
            clob=clob, token_id="t1", side="BUY",
            whale_price=0.50, tolerance_pct=0.03, max_spread=0.02,
        )


async def test_no_max_spread_skips_check():
    # Sem max_spread fornecido → só Regra 1 vale.
    clob = _FakeCLOB({"asks": [{"price": "0.55"}], "bids": [{"price": "0.40"}]})
    # whale 0.60 → tolerance 3% → 0.582 mínimo. Bid 0.40 → aborta por slippage.
    with pytest.raises(SlippageExceededError):
        await check_slippage_or_abort(
            clob=clob, token_id="t1", side="SELL",
            whale_price=0.60, tolerance_pct=0.03, max_spread=None,
        )
