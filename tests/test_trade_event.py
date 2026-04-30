"""TradeEvent + parse_trade_event — paridade com legacy + novos campos."""
from __future__ import annotations

import pytest

from src.core.trade_event import TradeEvent, parse_trade_event


def test_parse_minimal_valid_payload():
    evt = parse_trade_event({
        "maker": "0xWHALE",
        "conditionId": "0xC1",
        "asset": "tok-yes",
        "side": "BUY",
        "size": "100",
        "price": "0.42",
        "timestamp": 1_700_000_000,
    })
    assert evt is not None
    assert evt.wallet == "0xwhale"  # lowercased
    assert evt.condition_id == "0xC1"
    assert evt.token_id == "tok-yes"
    assert evt.side == "BUY"
    assert evt.size == 100.0
    assert evt.price == 0.42
    assert evt.usd_value == pytest.approx(42.0)
    assert evt.current_whale_size is None  # ausente


def test_parse_picks_camelcase_alternates():
    """RTDS 2026 usa makerAddress + tokenId; deve cair pra esses aliases."""
    evt = parse_trade_event({
        "makerAddress": "0xABC",
        "condition_id": "cid",
        "tokenId": "tok",
        "side": "sell",  # case insensitive
        "size": 5,
        "price": 0.5,
    })
    assert evt is not None
    assert evt.side == "SELL"
    assert evt.token_id == "tok"


def test_parse_returns_none_for_missing_critical():
    # Sem maker
    assert parse_trade_event({
        "conditionId": "c", "asset": "t", "side": "BUY", "size": 1, "price": 0.5,
    }) is None
    # Sem side válido
    assert parse_trade_event({
        "maker": "0xa", "conditionId": "c", "asset": "t",
        "side": "INVALID", "size": 1, "price": 0.5,
    }) is None
    # Sem token_id
    assert parse_trade_event({
        "maker": "0xa", "conditionId": "c", "side": "BUY", "size": 1, "price": 0.5,
    }) is None


def test_parse_extracts_current_whale_size_from_aliases():
    for alias in ("currentSize", "balanceAfter", "postSize", "makerBalance"):
        evt = parse_trade_event({
            "maker": "0xa", "conditionId": "c", "asset": "t",
            "side": "SELL", "size": 50, "price": 0.4,
            alias: "12.5",
        })
        assert evt is not None, f"alias {alias} não foi reconhecido"
        assert evt.current_whale_size == 12.5


def test_parse_handles_str_numerics():
    """RTDS textual emite size/price como strings — deve converter."""
    evt = parse_trade_event({
        "maker": "0xa", "conditionId": "c", "asset": "t",
        "side": "BUY", "size": "100.5", "price": "0.33",
    })
    assert evt is not None
    assert evt.size == 100.5
    assert evt.price == 0.33


def test_parse_falls_back_to_usdSize_when_price_zero():
    evt = parse_trade_event({
        "maker": "0xa", "conditionId": "c", "asset": "t",
        "side": "BUY", "size": 100, "price": 0,
        "usdSize": "42.5",
    })
    assert evt is not None
    assert evt.usd_value == 42.5


def test_trade_event_is_frozen_and_slotted():
    evt = parse_trade_event({
        "maker": "0xa", "conditionId": "c", "asset": "t",
        "side": "BUY", "size": 1, "price": 0.5,
    })
    assert evt is not None
    # Frozen — atribuição direta deve falhar.
    with pytest.raises(Exception):
        evt.wallet = "0xb"  # type: ignore[misc]
    # Slots — sem __dict__.
    assert not hasattr(evt, "__dict__")
