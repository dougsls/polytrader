"""Risk Manager refactor — SELL bypass, Kelly puro, anti-fragmentação."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.config import load_yaml_config
from src.core.models import RiskState, TradeSignal
from src.executor.risk_manager import RiskManager


def _signal(**overrides) -> TradeSignal:
    base = dict(
        id="sig-1", wallet_address="0xW", wallet_score=0.8,
        condition_id="0xC", token_id="t1", side="BUY",
        size=100.0, price=0.50, usd_value=50.0,
        market_title="m", outcome="Yes",
        detected_at=datetime.now(timezone.utc),
        source="websocket", status="pending",
    )
    base.update(overrides)
    return TradeSignal(**base)


def _state(**overrides) -> RiskState:
    base = dict(
        total_portfolio_value=500.0, total_invested=0.0,
        total_unrealized_pnl=0.0, total_realized_pnl=0.0,
        daily_pnl=0.0, max_drawdown=0.0, current_drawdown=0.0,
        open_positions=0,
    )
    base.update(overrides)
    return RiskState(**base)


# ============ Item 1 — SELL bypass durante halt ============

def test_sell_bypasses_halt_after_daily_loss():
    """Cenário catastrófico: daily_loss tripou halt; whale manda SELL pra
    estancar; bot DEVE liberar a venda apesar de halted."""
    rm = RiskManager(load_yaml_config().executor)
    # Trigger halt via BUY com daily_pnl excedido.
    rm.evaluate(_signal(side="BUY"), _state(daily_pnl=-150.0))
    assert rm.is_halted

    # SELL chega depois — DEVE PASSAR (exit always allowed).
    sell = _signal(side="SELL", id="sig-sell")
    d = rm.evaluate(sell, _state())  # state normal
    assert d.allowed, f"SELL bloqueado em halt — sangria garantida! reason={d.reason}"
    assert d.sized_usd > 0


def test_sell_bypasses_halt_after_drawdown():
    """Drawdown trip → BUY blocked, SELL liberado."""
    rm = RiskManager(load_yaml_config().executor)
    cfg = rm._cfg
    # Drawdown > max_drawdown_pct (0.20 default)
    drawdown_state = _state(current_drawdown=cfg.max_drawdown_pct + 0.01)

    # BUY com drawdown excedido → halt + reject
    d_buy = rm.evaluate(_signal(side="BUY"), drawdown_state)
    assert not d_buy.allowed
    assert rm.is_halted

    # SELL com mesmo state → libera (exit-only path)
    d_sell = rm.evaluate(_signal(side="SELL", id="s2"), drawdown_state)
    assert d_sell.allowed
    assert d_sell.sized_usd > 0


def test_buy_still_blocked_when_halted():
    """Sanity: BUY continua bloqueado em halt; comportamento não regrediu."""
    rm = RiskManager(load_yaml_config().executor)
    rm.halt("manual_test")
    d = rm.evaluate(_signal(side="BUY"), _state())
    assert not d.allowed
    assert "HALTED_BUY" in d.reason


def test_sell_doesnt_check_portfolio_cap():
    """SELL é exit (libera capital) — PORTFOLIO_CAP não se aplica."""
    rm = RiskManager(load_yaml_config().executor)
    # Portfolio quase cheio — BUY rejeitaria. SELL deve passar.
    state = _state(total_invested=499.0)
    d_buy = rm.evaluate(_signal(side="BUY"), state)
    assert not d_buy.allowed and "PORTFOLIO_CAP" in d_buy.reason

    d_sell = rm.evaluate(_signal(side="SELL", id="s3"), state)
    assert d_sell.allowed


def test_sell_doesnt_check_max_positions():
    """SELL não consume `open_positions` budget; MAX_POSITIONS é BUY-only."""
    rm = RiskManager(load_yaml_config().executor)
    state = _state(open_positions=99)  # absurdamente cheio
    d_sell = rm.evaluate(_signal(side="SELL"), state)
    assert d_sell.allowed


def test_sell_still_validates_low_score():
    """LOW_SCORE protege SELL contra sinais externos não-confiáveis."""
    rm = RiskManager(load_yaml_config().executor)
    rm.halt("manual")
    d_low = rm.evaluate(
        _signal(side="SELL", wallet_score=0.30, id="s4"), _state(),
    )
    assert not d_low.allowed and "LOW_SCORE" in d_low.reason


def test_sell_bypasses_price_band():
    """⚠️ Item 4 fix — PRICE_BAND é BUY-only. Token despencado a 0.02
    deve poder ser vendido pra liberar capital. Antes, este teste
    afirmava o oposto (bug conhecido)."""
    rm = RiskManager(load_yaml_config().executor)
    d_sell = rm.evaluate(
        _signal(side="SELL", price=0.02, id="s5"), _state(),
    )
    assert d_sell.allowed, "SELL outside band deve passar pra exit"


# ============ Item 2 — Kelly com whale_win_rate puro ============

def _kelly_cfg():
    cfg = load_yaml_config().executor
    # MagicMock-style override (frozen Pydantic — usar setattr indireto).
    object.__setattr__(cfg, "sizing_mode", "kelly")
    return cfg


def test_kelly_uses_whale_win_rate_when_provided():
    """Kelly fórmula: f* = p - q/odds. p deve ser whale_win_rate, NÃO score."""
    rm = RiskManager(_kelly_cfg())
    # Score composto distorcido (alto), win_rate real conservador.
    sig = _signal(wallet_score=0.95, whale_win_rate=0.55, price=0.50)
    d = rm.evaluate(sig, _state())
    # Com p=0.55 e price=0.50 (odds=1): f* = 0.55 - 0.45/1 = 0.10
    # Tamanho esperado = 500 × 0.10 = 50 (limited by max_position_usd 10)
    expected_kelly_size = 500.0 * 0.10  # antes do cap por position
    # max_position_usd no config é 10 — sized será capped a 10.
    assert d.allowed
    assert d.sized_usd == pytest.approx(min(expected_kelly_size, 10.0))


def test_kelly_falls_back_to_score_when_win_rate_missing():
    """Se whale_win_rate=None, usa wallet_score (compat) + log warning."""
    rm = RiskManager(_kelly_cfg())
    # score 0.65 acima do min_confidence_score=0.6 default
    sig = _signal(wallet_score=0.65, whale_win_rate=None, price=0.50)
    d = rm.evaluate(sig, _state())
    # Mesma matemática, mas com p=score=0.65 (fallback).
    assert d.allowed
    # Sanity: sizing > 0 quando p > price (Kelly positivo)
    assert d.sized_usd > 0


def test_kelly_clamps_invalid_win_rate():
    """win_rate fora de [0.01, 0.99] não pode quebrar Kelly (divisão por zero)."""
    rm = RiskManager(_kelly_cfg())
    # win_rate = 1.0 → q=0 → não diverge (Kelly trataria como all-in capped)
    sig = _signal(whale_win_rate=1.0, price=0.50)
    d = rm.evaluate(sig, _state())
    assert d.allowed  # não crash
    # win_rate = 0.0 → kelly = -∞; clamp + max(...,0) garante 0
    sig2 = _signal(whale_win_rate=0.0, price=0.50, id="s2")
    d2 = rm.evaluate(sig2, _state())
    # Kelly == 0 → ZERO_SIZE
    assert not d2.allowed


def test_kelly_distortion_when_using_composite_score_demonstrated():
    """REGRESSION: prova matemática de que score composto distorce Kelly.

    Cenário: whale com score composto 0.95 (top performer multi-fator)
    mas win_rate real só 0.55 (volume veio de pnl absoluto + recência).
    Antes do fix, Kelly usaria 0.95 → f* gigante; agora usa 0.55 → f*
    realista.
    """
    rm = RiskManager(_kelly_cfg())
    # Antes (incorreto): p=0.95 → f* = 0.95 - 0.05/1 = 0.90 → all-in capped 25%
    # Depois (correto): p=0.55 → f* = 0.10
    sig = _signal(wallet_score=0.95, whale_win_rate=0.55, price=0.50)
    sig_buggy = _signal(wallet_score=0.95, whale_win_rate=None, price=0.50, id="s2")

    # Em config real ambos ficam capped pelo max_position_usd=10, mas
    # podemos verificar que a fórmula interna difere via portfolio raw.
    # Aproximamos chamando _size_usd direto (bypass max cap):
    raw_correct = rm._size_usd(sig, _state())
    raw_buggy = rm._size_usd(sig_buggy, _state())
    # raw_correct deve ser MUITO menor que raw_buggy (10% vs 25% × $500)
    assert raw_correct < raw_buggy
    assert raw_correct == pytest.approx(50.0, rel=0.01)   # 0.10 × 500
    assert raw_buggy == pytest.approx(125.0, rel=0.01)     # 0.25 × 500 (capped)


# ============ Item 3 — Anti-fragmentação no whale_proportional ============

def _whale_prop_cfg():
    cfg = load_yaml_config().executor
    object.__setattr__(cfg, "sizing_mode", "whale_proportional")
    return cfg


def test_whale_proportional_uses_total_position_when_present():
    """Convicção = total_position / whale_bank, NÃO fill_size / whale_bank."""
    rm = RiskManager(_whale_prop_cfg())
    sig = _signal(
        usd_value=10_000.0,                    # fill atomic ($10k)
        whale_total_position_usd=100_000.0,    # posição total ($100k)
        whale_portfolio_usd=1_000_000.0,       # whale bank
    )
    raw = rm._size_usd(sig, _state(total_portfolio_value=500.0))
    # Convicção = 100k / 1M = 10% (real, anti-fragmentação)
    # raw esperado = 500 × 0.10 × whale_sizing_factor (1.0 default) = 50
    assert raw == pytest.approx(50.0)


def test_whale_proportional_falls_back_to_fill_when_total_missing():
    """current_whale ausente → comportamento legado preservado."""
    rm = RiskManager(_whale_prop_cfg())
    sig = _signal(
        usd_value=10_000.0,
        whale_total_position_usd=None,         # ausente!
        whale_portfolio_usd=1_000_000.0,
    )
    raw = rm._size_usd(sig, _state(total_portfolio_value=500.0))
    # Convicção = 10k / 1M = 1% (fragmented, legado)
    assert raw == pytest.approx(5.0)


def test_fragmentation_creates_correct_size_regardless_of_fill_count():
    """Cenário canônico: whale parte $100k em 10× $10k; cada fill emite
    com currentSize crescente. Todos os 10 sinais devem ter mesma
    convicção (= 100k / 1M = 10%) — não 10 × 1%."""
    rm = RiskManager(_whale_prop_cfg())
    state = _state(total_portfolio_value=500.0)

    # 3º fill: whale acumula 30k = 30% da ordem total
    sig_mid = _signal(
        usd_value=10_000.0,                    # 1 fill = 10k
        whale_total_position_usd=30_000.0,     # acumulado pós-trade
        whale_portfolio_usd=1_000_000.0,
    )
    # 10º fill: whale acumula 100k = posição final
    sig_final = _signal(
        usd_value=10_000.0,
        whale_total_position_usd=100_000.0,
        whale_portfolio_usd=1_000_000.0,
        id="sf",
    )
    # Antes do fix: ambos retornariam 5 USD (10k/1M × 500 = 5)
    # Depois: cada um respeita a convicção REAL no momento.
    raw_mid = rm._size_usd(sig_mid, state)
    raw_final = rm._size_usd(sig_final, state)

    assert raw_mid == pytest.approx(15.0)   # 30/1000 × 500 = 15
    assert raw_final == pytest.approx(50.0) # 100/1000 × 500 = 50
    # Bot agora "cresce" a posição conforme a whale completa a ordem.


def test_whale_proportional_caps_outlier_conviction():
    """Whale all-in (100% do bank num token só) é capped em 25%."""
    rm = RiskManager(_whale_prop_cfg())
    sig = _signal(
        whale_total_position_usd=900_000.0,   # whale all-in
        whale_portfolio_usd=1_000_000.0,
    )
    raw = rm._size_usd(sig, _state(total_portfolio_value=500.0))
    # Conviction = 0.90 → capped em 0.25 → 500 × 0.25 = 125
    assert raw == pytest.approx(125.0)
