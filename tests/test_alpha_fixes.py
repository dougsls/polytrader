"""5 correções de Alpha Generation — testes integrados.

Cobre:
  Item 1: consistency real (não clone do win_rate)
  Item 2: recent_pnl_7d gate ("on tilt" detection)
  Item 3: anti-correlação (mesma condition, token oposto)
  Item 4: confluence tracker + amplifier
  Item 5: exclude_hyperactive heurística trades/markets
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.config import load_yaml_config
from src.core.models import RiskState, TradeSignal
from src.core.state import InMemoryState
from src.executor.risk_manager import RiskManager
from src.scanner.profiler import WalletProfile
from src.scanner.scorer import (
    _consistency_score,
    _is_hyperactive,
    score_wallet,
)
from src.tracker.confluence import ConfluenceTracker


def _profile(**overrides) -> WalletProfile:
    base = dict(
        address="0xWHALE",
        name="test",
        pnl_usd=5_000.0,
        volume_usd=50_000.0,  # ratio 0.10 > 0.05 (passa wash filter)
        win_rate=0.65,
        total_trades=30,
        distinct_markets=10,
        short_term_trade_ratio=0.80,
        last_trade_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    base.update(overrides)
    return WalletProfile(**base)


# ============ Item 1 — Consistency real ============

def test_consistency_uses_sample_size_not_win_rate():
    """consistency = total_trades/50 capped 1.0 — NÃO clone do win_rate."""
    assert _consistency_score(0) == 0.0
    assert _consistency_score(25) == 0.5
    assert _consistency_score(50) == 1.0
    assert _consistency_score(500) == 1.0  # capped


def test_score_uses_independent_consistency():
    """Score com consistency separada não infla mais o win_rate."""
    cfg = load_yaml_config().scanner
    # Whale com win_rate alta MAS poucos trades → consistency baixa
    p_low_sample = _profile(win_rate=0.85, total_trades=10)
    s_low = score_wallet(p_low_sample, cfg)

    # Whale com win_rate similar E muitos trades → consistency alta
    p_high_sample = _profile(win_rate=0.85, total_trades=100)
    s_high = score_wallet(p_high_sample, cfg)

    # Score deve ser MAIOR no high_sample (consistency real conta).
    # Antes do fix, ambos teriam o mesmo score (clone do win_rate).
    assert s_high > s_low


# ============ Item 5 — exclude_hyperactive ============

def test_is_hyperactive_detects_high_trades_per_market():
    """>50 trades/mercado em média = wash/MM."""
    p_normal = _profile(total_trades=30, distinct_markets=10)  # 3/market
    p_mm = _profile(total_trades=300, distinct_markets=3)      # 100/market
    p_borderline = _profile(total_trades=500, distinct_markets=10)  # 50/market
    assert _is_hyperactive(p_normal) is False
    assert _is_hyperactive(p_mm) is True
    assert _is_hyperactive(p_borderline) is False  # 50 não é > 50


def test_is_hyperactive_handles_zero_markets():
    """distinct_markets=0 não deve crashar (defesa contra divide-by-zero)."""
    p_zero = _profile(total_trades=10, distinct_markets=0)
    assert _is_hyperactive(p_zero) is False


def test_score_zero_for_hyperactive_when_filter_enabled():
    """Hyperactive recebe score=0 quando filter ativo."""
    cfg = load_yaml_config().scanner
    # Garantir que exclude_hyperactive=True (default em config.yaml)
    assert cfg.wash_trading_filter.exclude_hyperactive is True
    p_mm = _profile(total_trades=500, distinct_markets=2)  # 250/market
    assert score_wallet(p_mm, cfg) == 0.0


# ============ Item 2 — recent_pnl_7d "on tilt" gate ============

def test_score_penalizes_negative_recent_pnl():
    """Whale perdendo nos 7d → score × 0.3."""
    cfg = load_yaml_config().scanner
    p_winning = _profile(recent_pnl_7d=500.0)
    p_tilt = _profile(recent_pnl_7d=-500.0)
    p_no_data = _profile(recent_pnl_7d=None)

    s_win = score_wallet(p_winning, cfg)
    s_tilt = score_wallet(p_tilt, cfg)
    s_no_data = score_wallet(p_no_data, cfg)

    # Whale "on tilt" é severamente penalizada (mas não zerada).
    assert s_tilt < s_win
    assert s_tilt == pytest.approx(s_win * 0.3, rel=0.01)
    # Sem dados (None) NÃO ativa o gate (compat retroativa).
    assert s_no_data == s_win


def test_score_no_penalty_for_zero_recent_pnl():
    """recent_pnl_7d == 0 NÃO ativa penalty (precisa ser <0)."""
    cfg = load_yaml_config().scanner
    p_zero = _profile(recent_pnl_7d=0.0)
    p_winning = _profile(recent_pnl_7d=100.0)
    assert score_wallet(p_zero, cfg) == score_wallet(p_winning, cfg)


# ============ Item 3 — Anti-correlação ============

def test_state_detects_conflicting_position():
    """has_conflicting_position retorna True quando há outro token no
    mesmo condition_id."""
    s = InMemoryState()
    s.bot_set("tok-yes", 100.0, condition_id="0xCID")
    # Mesmo condition, OUTRO token → conflito
    assert s.has_conflicting_position("0xCID", "tok-no") is True
    # Mesmo condition, MESMO token → não é conflito (apenas adicionando)
    assert s.has_conflicting_position("0xCID", "tok-yes") is False
    # Outro condition → sem conflito
    assert s.has_conflicting_position("0xOTHER", "tok-x") is False


def test_state_clears_conflict_after_position_close():
    """Quando size cai a 0, condition_to_tokens é limpo."""
    s = InMemoryState()
    s.bot_set("tok-yes", 100.0, condition_id="0xCID")
    assert s.has_conflicting_position("0xCID", "tok-no") is True
    # Fecha a posição
    s.bot_set("tok-yes", 0.0, condition_id="0xCID")
    # Conflito desaparece
    assert s.has_conflicting_position("0xCID", "tok-no") is False
    # Mapping limpo
    assert "0xCID" not in s.condition_to_tokens


def test_bot_add_propagates_condition_id():
    """bot_add com condition_id mantém condition_to_tokens em sync."""
    s = InMemoryState()
    s.bot_add("tok-1", 100.0, condition_id="0xC")
    assert s.bot_size("tok-1") == 100.0
    assert "tok-1" in s.condition_to_tokens.get("0xC", set())
    # Decremento total → cleanup
    s.bot_add("tok-1", -100.0, condition_id="0xC")
    assert s.bot_size("tok-1") == 0.0
    assert "0xC" not in s.condition_to_tokens


@pytest.mark.asyncio
async def test_detect_signal_blocks_conflicting_buy():
    """detect_signal rejeita BUY quando bot tem outro token no mesmo cid."""
    from src.tracker.signal_detector import detect_signal

    state = InMemoryState()
    # Bot já tem YES aberto — whale tenta comprar NO
    state.bot_set("tok-yes", 100.0, condition_id="0xCID")

    cfg = MagicMock()
    cfg.signal_max_age_seconds = 3600
    cfg.min_trade_size_usd = 0
    cfg.market_duration_filter.enabled = False

    gamma = MagicMock()
    gamma.get_market = AsyncMock(return_value={"tokens": []})

    sig = await detect_signal(
        trade={
            "maker": "0xWHALE", "conditionId": "0xCID", "asset": "tok-no",
            "side": "BUY", "size": 50, "price": 0.40,
            "timestamp": int(datetime.now(timezone.utc).timestamp()),
        },
        wallet_score=0.8, cfg=cfg, gamma=gamma,
        data_client=MagicMock(), state=state,
    )
    assert sig is None  # bloqueado por conflito


@pytest.mark.asyncio
async def test_detect_signal_allows_same_token_buy():
    """BUY do MESMO token (acumulando) NÃO é conflito."""
    from src.tracker.signal_detector import detect_signal

    state = InMemoryState()
    state.bot_set("tok-yes", 100.0, condition_id="0xCID")

    cfg = MagicMock()
    cfg.signal_max_age_seconds = 3600
    cfg.min_trade_size_usd = 0
    cfg.market_duration_filter.enabled = False

    gamma = MagicMock()
    gamma.get_market = AsyncMock(return_value={"tokens": []})

    # Whale comprando MAIS YES (token já detido)
    sig = await detect_signal(
        trade={
            "maker": "0xWHALE", "conditionId": "0xCID", "asset": "tok-yes",
            "side": "BUY", "size": 50, "price": 0.50,
            "timestamp": int(datetime.now(timezone.utc).timestamp()),
        },
        wallet_score=0.8, cfg=cfg, gamma=gamma,
        data_client=MagicMock(), state=state,
    )
    assert sig is not None  # permitido — não é conflito


@pytest.mark.asyncio
async def test_detect_signal_allows_sell_even_with_other_token():
    """SELL não é bloqueado pelo gate (é exit, não entrada nova)."""
    from src.tracker.signal_detector import detect_signal

    state = InMemoryState()
    state.bot_set("tok-yes", 100.0, condition_id="0xCID")
    # Por contexto, hipotético: bot também tinha algum NO; whale sai
    state.whale_set("0xwhale", "tok-no", 200.0)
    state.bot_set("tok-no", 50.0, condition_id="0xCID")

    cfg = MagicMock()
    cfg.signal_max_age_seconds = 3600
    cfg.min_trade_size_usd = 0
    cfg.market_duration_filter.enabled = False

    gamma = MagicMock()
    gamma.get_market = AsyncMock(return_value={"tokens": []})

    sig = await detect_signal(
        trade={
            "maker": "0xWHALE", "conditionId": "0xCID", "asset": "tok-no",
            "side": "SELL", "size": 100, "price": 0.40,
            "timestamp": int(datetime.now(timezone.utc).timestamp()),
        },
        wallet_score=0.8, cfg=cfg, gamma=gamma,
        data_client=MagicMock(), state=state,
    )
    # SELL passa pelo Exit Sync normalmente (gate só pra BUY)
    assert sig is not None


# ============ Item 4 — Confluence Tracker ============

def test_confluence_first_whale_returns_one():
    ct = ConfluenceTracker()
    assert ct.record("0xC", "BUY", "0xWA") == 1


def test_confluence_two_distinct_whales_returns_two():
    ct = ConfluenceTracker()
    ct.record("0xC", "BUY", "0xWA")
    assert ct.record("0xC", "BUY", "0xWB") == 2


def test_confluence_same_whale_twice_doesnt_double_count():
    """Re-record da mesma wallet refresca timestamp mas count=1."""
    ct = ConfluenceTracker()
    ct.record("0xC", "BUY", "0xWA")
    assert ct.record("0xC", "BUY", "0xWA") == 1
    assert ct.record("0xC", "BUY", "0xwa") == 1  # case-insensitive


def test_confluence_separate_buckets_per_side():
    """BUY YES e BUY NO no mesmo cid são contagens separadas."""
    ct = ConfluenceTracker()
    ct.record("0xC", "BUY", "0xWA")
    # SELL é outra chave
    assert ct.record("0xC", "SELL", "0xWB") == 1


def test_confluence_evicts_outside_window():
    """Wallet com ts antigo (fora da janela) é dropada."""
    ct = ConfluenceTracker(window_seconds=0.05)  # 50ms para teste rápido
    ct.record("0xC", "BUY", "0xWA")
    import time
    time.sleep(0.06)
    # Após sleep, WA deve ter expirado.
    count = ct.record("0xC", "BUY", "0xWB")
    assert count == 1  # só WB; WA foi dropada


def test_confluence_count_doesnt_register():
    """count() lê sem inserir nova whale."""
    ct = ConfluenceTracker()
    ct.record("0xC", "BUY", "0xWA")
    assert ct.count("0xC", "BUY") == 1
    assert ct.count("0xC", "BUY") == 1  # idempotente


def test_risk_manager_amplifies_sized_with_confluence():
    """sizing_mode=fixed: 2 whales convergindo → +25% sized."""
    cfg = load_yaml_config().executor
    rm = RiskManager(cfg)

    sig_solo = TradeSignal.model_construct(
        id="s1", wallet_address="0xa", wallet_score=0.8,
        condition_id="0xC", token_id="t", side="BUY",
        size=10.0, price=0.5, usd_value=5.0,
        market_title="m", outcome="Yes",
        market_end_date=None, hours_to_resolution=24.0,
        detected_at=datetime.now(timezone.utc),
        source="websocket", status="pending", skip_reason=None,
        confluence_count=1,
    )
    sig_conf2 = sig_solo.model_copy(update={"id": "s2", "confluence_count": 2})
    sig_conf3 = sig_solo.model_copy(update={"id": "s3", "confluence_count": 3})
    sig_max = sig_solo.model_copy(update={"id": "s4", "confluence_count": 100})

    state = RiskState(
        total_portfolio_value=500.0, total_invested=0.0,
        total_unrealized_pnl=0.0, total_realized_pnl=0.0,
        daily_pnl=0.0, max_drawdown=0.0, current_drawdown=0.0,
        open_positions=0,
    )

    raw_solo = rm._size_usd(sig_solo, state)
    raw_2 = rm._size_usd(sig_conf2, state)
    raw_3 = rm._size_usd(sig_conf3, state)
    raw_max = rm._size_usd(sig_max, state)

    # 1 whale: multiplier=1.0
    # 2 whales: 1 + 0.25 = 1.25
    # 3 whales: 1 + 0.50 = 1.50
    # 100 whales: capped 2.0
    assert raw_2 == pytest.approx(raw_solo * 1.25)
    assert raw_3 == pytest.approx(raw_solo * 1.50)
    assert raw_max == pytest.approx(raw_solo * 2.0)


def test_risk_manager_does_not_amplify_sell():
    """SELL é exit-driven, não amplifica por confluence."""
    cfg = load_yaml_config().executor
    rm = RiskManager(cfg)

    sig_buy = TradeSignal.model_construct(
        id="b1", wallet_address="0xa", wallet_score=0.8,
        condition_id="0xC", token_id="t", side="BUY",
        size=10.0, price=0.5, usd_value=5.0,
        market_title="m", outcome="Yes",
        market_end_date=None, hours_to_resolution=24.0,
        detected_at=datetime.now(timezone.utc),
        source="websocket", status="pending", skip_reason=None,
        confluence_count=3,
    )
    sig_sell = sig_buy.model_copy(update={"id": "s1", "side": "SELL"})

    state = RiskState(
        total_portfolio_value=500.0, total_invested=0.0,
        total_unrealized_pnl=0.0, total_realized_pnl=0.0,
        daily_pnl=0.0, max_drawdown=0.0, current_drawdown=0.0,
        open_positions=0,
    )

    raw_buy = rm._size_usd(sig_buy, state)
    raw_sell = rm._size_usd(sig_sell, state)
    # BUY: amplificado (× 1.5)
    # SELL: NÃO amplificado (mesmo size base)
    assert raw_buy > raw_sell
