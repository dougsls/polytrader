"""QUANT — VWAP math em scanner. Cobre o falso-positivo Top of Book."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.arbitrage.scanner import ArbScanner, _walk_vwap


# ============ _walk_vwap — função pura ============

def test_walk_empty_book_returns_zero():
    vwap, fill, depth, levels = _walk_vwap({}, "asks", 100.0, 5)
    assert vwap == 0.0 and fill == 0.0 and depth == 0.0 and levels == 0


def test_walk_single_level_partial_fill():
    """target dentro do L1 → vwap = price exato do L1."""
    book = {"asks": [{"price": "0.40", "size": "100"}]}
    vwap, fill, depth, levels = _walk_vwap(book, "asks", target_tokens=50, max_levels=5)
    assert vwap == 0.40
    assert fill == 50.0
    assert depth == 20.0
    assert levels == 1


def test_walk_consumes_multiple_levels_when_l1_too_shallow():
    """L1 com 10 tokens, target 30 → walk pra L2 e L3."""
    book = {"asks": [
        {"price": "0.30", "size": "10"},   # L1: 10 tokens × 0.30 = 3 USD
        {"price": "0.40", "size": "20"},   # L2: 20 tokens × 0.40 = 8 USD
        {"price": "0.55", "size": "100"},  # L3: não usado
    ]}
    vwap, fill, depth, levels = _walk_vwap(book, "asks", target_tokens=30, max_levels=5)
    # 10@0.30 + 20@0.40 = 11 USD / 30 tokens = 0.3667
    assert vwap == pytest.approx(11.0 / 30.0)
    assert fill == 30.0
    assert depth == pytest.approx(11.0)
    assert levels == 2


def test_walk_capped_by_max_levels():
    """Books com 50+ níveis não devem ser percorridos por inteiro."""
    book = {"asks": [
        {"price": str(0.10 + i * 0.01), "size": "1"}
        for i in range(50)
    ]}
    vwap, fill, depth, levels = _walk_vwap(book, "asks", target_tokens=999, max_levels=3)
    assert levels == 3
    assert fill == 3.0
    # 0.10 + 0.11 + 0.12 = 0.33 ÷ 3 = 0.11
    assert vwap == pytest.approx(0.11)


def test_walk_infinite_target_returns_total_depth():
    """target=inf → walk até esgotar (limitado por max_levels)."""
    book = {"asks": [
        {"price": "0.20", "size": "5"},
        {"price": "0.25", "size": "10"},
    ]}
    vwap, fill, depth, levels = _walk_vwap(
        book, "asks", target_tokens=float("inf"), max_levels=5,
    )
    assert fill == 15.0  # esgotou
    assert vwap == pytest.approx((1.0 + 2.5) / 15.0)
    assert levels == 2


def test_walk_bids_uses_descending_order():
    """Bids: best = highest price; order pode vir embaralhada."""
    book = {"bids": [
        {"price": "0.40", "size": "10"},
        {"price": "0.50", "size": "5"},   # melhor bid
        {"price": "0.42", "size": "20"},
    ]}
    vwap, fill, depth, levels = _walk_vwap(book, "bids", target_tokens=10, max_levels=5)
    # Sorted desc: 0.50, 0.42, 0.40 → 5@0.50 + 5@0.42 = 4.6 / 10 = 0.46
    assert vwap == pytest.approx(0.46)
    assert fill == 10.0


# ============ Edge correctness — false positive elimination ============

def test_vwap_eliminates_top_of_book_false_positive():
    """O cenário CRÍTICO: best_ask aparenta arb, mas L2 mata o edge.

    Top of Book diria: edge = 1 - 0.30 - 0.30 = 40% (FAKE).
    VWAP real: 11 USD por par → 0.55/par → edge = 1 - 0.55 = 45%.

    Wait — esse cenário está bom. Vamos no oposto: edge falso onde
    L1 é 30+30 = 60% margem, mas L2/L3 (onde dinheiro real iria)
    matam o lucro.
    """
    # Book YES: L1=$3, L2/L3 ruins
    book_yes = {"asks": [
        {"price": "0.30", "size": "10"},
        {"price": "0.45", "size": "100"},
        {"price": "0.50", "size": "100"},
    ]}
    # Book NO: similar
    book_no = {"asks": [
        {"price": "0.30", "size": "10"},
        {"price": "0.45", "size": "100"},
        {"price": "0.50", "size": "100"},
    ]}

    # Top of Book diz: 1 - 0.30 - 0.30 = 40% edge!
    top_yes = float(book_yes["asks"][0]["price"])
    top_no = float(book_no["asks"][0]["price"])
    fake_edge = 1.0 - top_yes - top_no
    assert fake_edge == pytest.approx(0.40)

    # VWAP real para 100 tokens (queremos um lote sério):
    vwap_yes, _, _, _ = _walk_vwap(book_yes, "asks", target_tokens=100, max_levels=5)
    vwap_no, _, _, _ = _walk_vwap(book_no, "asks", target_tokens=100, max_levels=5)
    real_edge = 1.0 - vwap_yes - vwap_no

    # Real edge é MUITO menor que o aparente.
    # 10@0.30 + 90@0.45 = 3 + 40.5 = 43.5 / 100 = 0.435
    # 1 - 0.435 - 0.435 = 0.13 (13%)
    assert vwap_yes == pytest.approx(0.435)
    assert real_edge == pytest.approx(0.13)
    assert real_edge < fake_edge  # sanity — VWAP é mais conservador
    assert real_edge < 0.20  # falso positivo eliminado


# ============ Scanner _evaluate_market — integration ============

def _mk_scanner(*, max_per_op_usd=50.0, min_edge_pct=0.005,
                fee_per_leg=0.0, safety_buffer_pct=0.0,
                min_book_depth_usd=10.0, same_market_cooldown_seconds=60):
    cfg = MagicMock()
    cfg.enabled = True
    cfg.max_per_op_usd = max_per_op_usd
    cfg.min_edge_pct = min_edge_pct
    cfg.fee_per_leg = fee_per_leg
    cfg.safety_buffer_pct = safety_buffer_pct
    cfg.min_book_depth_usd = min_book_depth_usd
    cfg.same_market_cooldown_seconds = same_market_cooldown_seconds
    queue: asyncio.Queue = asyncio.Queue()
    return ArbScanner(
        cfg=cfg, gamma=MagicMock(), clob=MagicMock(),
        queue=queue, conn=MagicMock(),
    )


@pytest.mark.asyncio
async def test_evaluate_skips_when_real_edge_below_threshold():
    """Scanner NÃO emite quando VWAP real fica abaixo de min_edge_pct."""
    sc = _mk_scanner(min_edge_pct=0.20)  # exige 20% mínimo

    # Books com top of book aparentando 40% mas vwap real 13%
    book = {"asks": [
        {"price": "0.30", "size": "10"},
        {"price": "0.45", "size": "100"},
    ]}
    sc.clob.book = AsyncMock(side_effect=[book, book])

    op = await sc._evaluate_market({
        "conditionId": "0xC",
        "tokens": [
            {"token_id": "yes", "outcome": "Yes"},
            {"token_id": "no", "outcome": "No"},
        ],
    })
    assert op is None  # falso positivo bloqueado


@pytest.mark.asyncio
async def test_evaluate_emits_when_real_edge_above_threshold():
    """Edge legitimamente >= threshold gera op com VWAP nos campos."""
    sc = _mk_scanner(min_edge_pct=0.05, min_book_depth_usd=1.0)

    # Book bom: profundidade real de tokens a 0.40 e 0.40
    book = {"asks": [
        {"price": "0.40", "size": "1000"},   # liquid
    ]}
    sc.clob.book = AsyncMock(side_effect=[book, book])

    op = await sc._evaluate_market({
        "conditionId": "0xCID",
        "tokens": [
            {"token_id": "yes", "outcome": "Yes"},
            {"token_id": "no", "outcome": "No"},
        ],
        "question": "Will X happen?",
    })
    assert op is not None
    # ask_yes/ask_no agora carregam VWAP — neste book trivial, vwap == best.
    assert op.ask_yes == 0.40
    assert op.ask_no == 0.40
    assert op.edge_gross_pct == pytest.approx(0.20)
    assert op.suggested_size_usd > 0
