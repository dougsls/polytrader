"""Testes do scanner de arbitragem — depth helpers e edge calculation."""
from __future__ import annotations

from src.arbitrage.scanner import _depth_usd


def test_depth_usd_empty_book():
    assert _depth_usd({}, "asks", 5) == (0.0, 0.0)
    assert _depth_usd({"asks": []}, "asks", 5) == (0.0, 0.0)


def test_depth_usd_asks_returns_best_and_sum():
    book = {
        "asks": [
            {"price": "0.40", "size": "100"},   # 40 USD
            {"price": "0.41", "size": "200"},   # 82 USD
            {"price": "0.42", "size": "50"},    # 21 USD
        ]
    }
    best, depth = _depth_usd(book, "asks", max_levels=3)
    assert best == 0.40
    # 40 + 82 + 21 = 143
    assert abs(depth - 143.0) < 1e-9


def test_depth_usd_respects_max_levels():
    book = {
        "asks": [
            {"price": "0.40", "size": "100"},
            {"price": "0.41", "size": "100"},
            {"price": "0.42", "size": "100"},
        ]
    }
    best, depth = _depth_usd(book, "asks", max_levels=1)
    assert best == 0.40
    assert depth == 40.0  # só primeiro nível


def test_depth_usd_bids_sorted_descending():
    # Mesmo se a API retornar fora de ordem, deve identificar best
    book = {
        "bids": [
            {"price": "0.30", "size": "50"},
            {"price": "0.35", "size": "100"},   # melhor bid
            {"price": "0.32", "size": "75"},
        ]
    }
    best, depth = _depth_usd(book, "bids", max_levels=3)
    assert best == 0.35
    assert abs(depth - (50 * 0.30 + 100 * 0.35 + 75 * 0.32)) < 1e-9
