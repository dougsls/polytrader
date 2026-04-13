"""Regra 3 — Detonação de Wash Traders: provas numéricas."""
from datetime import datetime, timedelta, timezone

from src.core.config import load_yaml_config
from src.scanner.profiler import WalletProfile
from src.scanner.scorer import score_wallet


def _healthy_profile(**overrides) -> WalletProfile:
    base = dict(
        address="0xWHALE",
        name="whale1",
        pnl_usd=5_000.0,
        volume_usd=20_000.0,  # V/PnL ratio = 0.25 → saudável
        win_rate=0.68,
        total_trades=80,
        distinct_markets=12,
        short_term_trade_ratio=0.75,
        last_trade_at=datetime.now(timezone.utc) - timedelta(hours=6),
    )
    base.update(overrides)
    return WalletProfile(**base)


def test_healthy_wallet_scores_positive():
    cfg = load_yaml_config().scanner
    score = score_wallet(_healthy_profile(), cfg)
    assert score > 0.0
    assert score <= 1.0


def test_wash_trader_hard_zero():
    """Carteira com pnl=$100 em $100k de volume (ratio 0.001) → score=0.

    min_volume_to_pnl_ratio no config.yaml é 0.05.
    """
    cfg = load_yaml_config().scanner
    profile = _healthy_profile(pnl_usd=1_000.0, volume_usd=100_000.0)  # ratio 0.01
    assert profile.volume_to_pnl_ratio < cfg.wash_trading_filter.min_volume_to_pnl_ratio
    assert score_wallet(profile, cfg) == 0.0


def test_below_min_profit_zero():
    cfg = load_yaml_config().scanner
    profile = _healthy_profile(pnl_usd=100.0)  # < 500 min
    assert score_wallet(profile, cfg) == 0.0


def test_below_win_rate_zero():
    cfg = load_yaml_config().scanner
    profile = _healthy_profile(win_rate=0.40)
    assert score_wallet(profile, cfg) == 0.0


def test_single_market_insider_zero():
    cfg = load_yaml_config().scanner
    profile = _healthy_profile(distinct_markets=1)
    assert score_wallet(profile, cfg) == 0.0


def test_low_short_term_ratio_zero():
    cfg = load_yaml_config().scanner
    profile = _healthy_profile(short_term_trade_ratio=0.20)  # < 0.50
    assert score_wallet(profile, cfg) == 0.0
