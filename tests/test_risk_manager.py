"""Risk Manager — gates e halt global."""
from datetime import datetime, timezone

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


def test_happy_path_allowed():
    rm = RiskManager(load_yaml_config().executor)
    d = rm.evaluate(_signal(), _state())
    assert d.allowed
    assert d.sized_usd > 0


def test_low_wallet_score_blocked():
    rm = RiskManager(load_yaml_config().executor)
    d = rm.evaluate(_signal(wallet_score=0.3), _state())
    assert not d.allowed
    assert "LOW_SCORE" in d.reason


def test_price_band_blocked():
    rm = RiskManager(load_yaml_config().executor)
    d = rm.evaluate(_signal(price=0.02), _state())  # < min_price 0.05
    assert not d.allowed


def test_daily_loss_halts():
    rm = RiskManager(load_yaml_config().executor)
    d = rm.evaluate(_signal(), _state(daily_pnl=-150.0))  # < -100 limit
    assert not d.allowed
    assert rm.is_halted
    # Próximo sinal também é bloqueado
    d2 = rm.evaluate(_signal(), _state())
    assert not d2.allowed
    assert "HALTED" in d2.reason


def test_portfolio_cap_blocked():
    rm = RiskManager(load_yaml_config().executor)
    d = rm.evaluate(_signal(), _state(total_invested=495.0))  # só sobram $5
    assert not d.allowed
    assert "PORTFOLIO_CAP" in d.reason


def test_max_positions_blocked_for_buy():
    rm = RiskManager(load_yaml_config().executor)
    d = rm.evaluate(_signal(), _state(open_positions=15))  # == max_positions
    assert not d.allowed
    assert "MAX_POSITIONS" in d.reason
