"""HFT — Item 2: Whale inventory drift correction.

Cobre os 3 caminhos de _compute_exit_size:
  - event_close: whale zerou (current=0) → bot vende TUDO
  - event_delta: current>0, prior>0 → matemática exata
  - size_proxy: current=None → fallback legado (vulnerável a drift)

E o efeito de drift correction no state RAM.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.state import InMemoryState
from src.tracker.signal_detector import _compute_exit_size, detect_signal


# ============ _compute_exit_size — função pura ============

def test_event_close_overrides_size():
    """current=0 → whale zerou. bot deve vender tudo, ignorando `size`."""
    adj, pct, src = _compute_exit_size(
        size=10, prior_whale=200, bot_size=50, current_whale=0,
    )
    assert adj == 50  # full bot_size
    assert pct == 1.0
    assert src == "event_close"


def test_event_delta_uses_prior_minus_current():
    """current>0 → matemática exata: (prior-current)/prior."""
    # Whale tinha 200, agora tem 50 → vendeu 150 (75%).
    adj, pct, src = _compute_exit_size(
        size=999,  # `size` é IGNORADO; usamos o delta real
        prior_whale=200, bot_size=10, current_whale=50,
    )
    assert pct == pytest.approx(0.75)
    assert adj == pytest.approx(7.5)
    assert src == "event_delta"


def test_event_delta_handles_current_greater_than_prior():
    """current > prior (whale comprou + nosso prior tava defasado).
    Deve clampar pct para 0 (não vendemos nada)."""
    adj, pct, src = _compute_exit_size(
        size=10, prior_whale=100, bot_size=20, current_whale=150,
    )
    assert pct == 0.0
    assert adj == 0.0
    assert src == "event_delta"


def test_size_proxy_fallback_when_current_none():
    """current=None → fallback legado: pct = size/prior."""
    adj, pct, src = _compute_exit_size(
        size=50, prior_whale=200, bot_size=10, current_whale=None,
    )
    assert pct == pytest.approx(0.25)
    assert adj == pytest.approx(2.5)
    assert src == "size_proxy"


def test_size_proxy_caps_pct_at_one():
    """size > prior_whale (drift) → cap em 100%."""
    adj, pct, src = _compute_exit_size(
        size=999, prior_whale=200, bot_size=10, current_whale=None,
    )
    assert pct == 1.0
    assert adj == 10  # full bot_size
    assert src == "size_proxy"


# ============ detect_signal — drift correction integration ============

def _make_cfg(
    market_filter_enabled: bool = False,
    signal_max_age_seconds: int = 3600,
    min_trade_size_usd: float = 0,
):
    cfg = MagicMock()
    cfg.signal_max_age_seconds = signal_max_age_seconds
    cfg.min_trade_size_usd = min_trade_size_usd
    cfg.market_duration_filter.enabled = market_filter_enabled
    return cfg


@pytest.mark.asyncio
async def test_detect_signal_full_exit_when_whale_zeroed():
    """Whale zerou → adjusted_size = bot_size completo."""
    state = InMemoryState()
    state.bot_set("tok-1", 100.0)         # bot tem 100 tokens
    state.whale_set("0xwhale", "tok-1", 200.0)  # whale tinha 200

    cfg = _make_cfg()
    gamma = MagicMock()
    gamma.get_market = AsyncMock(return_value={
        "tokens": [{"token_id": "tok-1", "outcome": "Yes"}],
        "question": "Will X?",
    })

    sig = await detect_signal(
        trade={
            "maker": "0xWHALE", "conditionId": "0xC", "asset": "tok-1",
            "side": "SELL", "size": "10",  # vendeu só 10 nesse evento
            "price": 0.5, "timestamp": int(datetime.now(timezone.utc).timestamp()),
            "currentSize": 0,             # ⚠️ mas zerou completo
        },
        wallet_score=0.8, cfg=cfg, gamma=gamma,
        data_client=MagicMock(), state=state,
    )
    assert sig is not None
    # Bot vende TUDO (100), não os 10 do evento.
    assert sig.size == 100.0
    # State foi atualizado com current=0 (whale removida).
    assert state.whale_size("0xwhale", "tok-1") == 0.0


@pytest.mark.asyncio
async def test_detect_signal_uses_event_delta_when_current_present():
    """current > 0 → pct = (prior - current) / prior."""
    state = InMemoryState()
    state.bot_set("tok-1", 40.0)
    state.whale_set("0xwhale", "tok-1", 200.0)

    cfg = _make_cfg()
    gamma = MagicMock()
    gamma.get_market = AsyncMock(return_value={"tokens": []})

    sig = await detect_signal(
        trade={
            "maker": "0xWHALE", "conditionId": "0xC", "asset": "tok-1",
            "side": "SELL", "size": "999",  # campo bagunçado de propósito
            "price": 0.5, "timestamp": int(datetime.now(timezone.utc).timestamp()),
            "currentSize": 50,  # whale agora tem 50 → vendeu 75%
        },
        wallet_score=0.8, cfg=cfg, gamma=gamma,
        data_client=MagicMock(), state=state,
    )
    assert sig is not None
    # 40 × 0.75 = 30
    assert sig.size == pytest.approx(30.0)
    # Drift correction: state agora reflete 50 (não 200 antigo).
    assert state.whale_size("0xwhale", "tok-1") == 50.0


@pytest.mark.asyncio
async def test_detect_signal_falls_back_to_size_proxy_without_current():
    """current ausente → cálculo legado size/prior."""
    state = InMemoryState()
    state.bot_set("tok-1", 40.0)
    state.whale_set("0xwhale", "tok-1", 200.0)

    cfg = _make_cfg()
    gamma = MagicMock()
    gamma.get_market = AsyncMock(return_value={"tokens": []})

    sig = await detect_signal(
        trade={
            "maker": "0xWHALE", "conditionId": "0xC", "asset": "tok-1",
            "side": "SELL", "size": 50, "price": 0.5,
            "timestamp": int(datetime.now(timezone.utc).timestamp()),
            # SEM currentSize
        },
        wallet_score=0.8, cfg=cfg, gamma=gamma,
        data_client=MagicMock(), state=state,
    )
    assert sig is not None
    # pct = 50/200 = 0.25; bot_size 40 × 0.25 = 10
    assert sig.size == pytest.approx(10.0)
    # State NÃO foi alterado (sem current pra validar).
    assert state.whale_size("0xwhale", "tok-1") == 200.0
