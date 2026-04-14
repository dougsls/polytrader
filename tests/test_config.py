"""Fase 1 — config.yaml + .env loader."""
from src.core.config import load_yaml_config, get_settings


def test_yaml_loads_and_validates():
    cfg = load_yaml_config()
    assert 0 < cfg.executor.whale_max_slippage_pct <= 1.0
    assert cfg.scanner.wash_trading_filter.enabled is True
    assert cfg.tracker.market_duration_filter.hard_block_days == 3


def test_scoring_weights_sum_to_one():
    cfg = load_yaml_config()
    w = cfg.scanner.scoring_weights
    total = w.pnl + w.win_rate + w.consistency + w.recency + w.market_diversity + w.short_term_ratio
    assert abs(total - 1.0) < 0.001


def test_settings_façade_is_singleton():
    a = get_settings()
    b = get_settings()
    assert a is b
