"""Testes do depth_sizing — Track B."""
from __future__ import annotations

from src.executor.depth_sizing import quote_buy_depth, quote_sell_depth


def test_buy_empty_book():
    q = quote_buy_depth({}, max_size_usd=100, max_impact_pct=0.02)
    assert q.fillable_size_usd == 0.0
    assert q.book_thin is True


def test_buy_within_first_level():
    book = {"asks": [{"price": "0.50", "size": "1000"}]}
    q = quote_buy_depth(book, max_size_usd=50, max_impact_pct=0.05)
    # cabe inteiro no primeiro nível (capacidade 500 usd)
    assert q.fillable_size_usd == 50.0
    assert q.vwap_price == 0.50
    assert q.levels_consumed == 1


def test_buy_capped_by_impact():
    # First level: 0.40, second level: 0.50 — 25% impact, max 5%.
    # Should stop at level 1.
    book = {
        "asks": [
            {"price": "0.40", "size": "10"},   # capacity 4 usd
            {"price": "0.50", "size": "100"},  # blocked by impact cap
        ]
    }
    q = quote_buy_depth(book, max_size_usd=1000, max_impact_pct=0.05)
    assert q.fillable_size_usd == 4.0
    assert q.vwap_price == 0.40


def test_buy_walks_book_within_impact():
    # First 0.40 (4 usd), second 0.41 (2.5% impact, OK at max 0.05),
    # third 0.43 (7.5% impact, bloqueia).
    book = {
        "asks": [
            {"price": "0.40", "size": "10"},   # 4 usd
            {"price": "0.41", "size": "10"},   # 4.1 usd
            {"price": "0.43", "size": "100"},  # blocked
        ]
    }
    q = quote_buy_depth(book, max_size_usd=1000, max_impact_pct=0.05)
    assert abs(q.fillable_size_usd - 8.1) < 1e-9
    assert q.levels_consumed == 2
    # vwap entre 0.40 e 0.41
    assert 0.40 < q.vwap_price < 0.41


def test_sell_empty_book():
    q = quote_sell_depth({}, max_size_usd=100, max_impact_pct=0.02)
    assert q.fillable_size_usd == 0.0
    assert q.book_thin is True


def test_sell_capped_by_impact():
    book = {
        "bids": [
            {"price": "0.60", "size": "10"},   # 6 usd
            {"price": "0.50", "size": "100"},  # 16.7% drop, cap 5% → bloqueia
        ]
    }
    q = quote_sell_depth(book, max_size_usd=1000, max_impact_pct=0.05)
    assert q.fillable_size_usd == 6.0
    assert q.best_price == 0.60


def test_sell_max_size_caps_before_impact():
    book = {
        "bids": [
            {"price": "0.60", "size": "1000"},  # capacity 600 usd
        ]
    }
    q = quote_sell_depth(book, max_size_usd=10, max_impact_pct=0.05)
    # max_size é o limitante (não impact)
    assert q.fillable_size_usd == 10.0
    assert q.vwap_price == 0.60
