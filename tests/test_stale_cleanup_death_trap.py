"""DEATH-TRAP FIX — stale cleanup com current_price + bypass slippage.

Cobre:
  - _synthetic_sell_signal usa current_price (não avg_price) quando disponível
  - bypass_slippage_check sempre True em sinais stale
  - copy_engine pula check_slippage_or_abort quando bypass=True
  - posição despencada (avg=0.50, current=0.05) NÃO trava no slippage
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.core.models import TradeSignal
from src.executor.stale_cleanup import _find_stale, _synthetic_sell_signal


# ============ _synthetic_sell_signal ============

def test_synthetic_signal_uses_current_price_as_anchor():
    """current_price disponível → anchor = current_price (NÃO avg)."""
    sig = _synthetic_sell_signal(
        "0xC", "tok", "Will X?", "Yes",
        size=100.0, avg_price=0.50, current_price=0.05,
    )
    assert sig.price == 0.05  # current, não avg
    assert sig.bypass_slippage_check is True
    assert sig.side == "SELL"
    assert sig.wallet_address == "__stale_cleanup__"
    # usd_value calculado em cima do anchor REAL
    assert sig.usd_value == pytest.approx(5.0)


def test_synthetic_signal_fallback_to_avg_when_current_none():
    """price_updater ainda não rodou → cai pro avg_price + bypass."""
    sig = _synthetic_sell_signal(
        "0xC", "tok", "Will X?", "Yes",
        size=100.0, avg_price=0.40, current_price=None,
    )
    assert sig.price == 0.40
    # Bypass garante que copy_engine não trava mesmo com fallback.
    assert sig.bypass_slippage_check is True


def test_synthetic_signal_fallback_when_current_zero_or_negative():
    """current_price=0 (book vazio) ou negativo → cai pro avg."""
    for invalid in (0.0, -0.1):
        sig = _synthetic_sell_signal(
            "0xC", "tok", "X?", "Yes",
            size=100.0, avg_price=0.40, current_price=invalid,
        )
        assert sig.price == 0.40
        assert sig.bypass_slippage_check is True


def test_synthetic_signal_score_bypasses_min_confidence():
    """wallet_score=1.0 garante que LOW_SCORE não bloqueia stale dump."""
    sig = _synthetic_sell_signal(
        "0xC", "tok", "X", "Yes", 100.0, 0.50, 0.05,
    )
    assert sig.wallet_score == 1.0


# ============ _find_stale com current_price ============

@pytest.mark.asyncio
async def test_find_stale_returns_current_price(tmp_path):
    """SELECT inclui current_price (coluna existente)."""
    from src.core.database import get_connection, init_database
    from datetime import timedelta

    db = tmp_path / "s.db"
    await init_database(db)
    # opened_at antigo (49h atrás → bate cutoff de 48h)
    old_iso = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()
    async with get_connection(db) as conn:
        await conn.execute(
            "INSERT INTO bot_positions"
            " (id, condition_id, token_id, market_title, outcome,"
            "  size, avg_entry_price, current_price, source_wallets_json,"
            "  opened_at, is_open, updated_at)"
            " VALUES (1, '0xC', 'tok', 'X', 'Yes',"
            "  100, 0.50, 0.05, '[]', ?, 1, ?)",
            (old_iso, now_iso),
        )
        await conn.commit()
        rows = await _find_stale(conn, max_age_hours=48.0)
    assert len(rows) == 1
    cid, tok, title, outcome, size, avg, cur = rows[0]
    assert avg == 0.50
    assert cur == 0.05  # current_price retornado


@pytest.mark.asyncio
async def test_find_stale_returns_none_for_uninitialized_current_price(tmp_path):
    """current_price NULL no DB → None na tupla."""
    from src.core.database import get_connection, init_database
    from datetime import timedelta

    db = tmp_path / "s.db"
    await init_database(db)
    old_iso = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()
    async with get_connection(db) as conn:
        # current_price omitido (NULL default)
        await conn.execute(
            "INSERT INTO bot_positions"
            " (id, condition_id, token_id, market_title, outcome,"
            "  size, avg_entry_price, source_wallets_json,"
            "  opened_at, is_open, updated_at)"
            " VALUES (2, '0xC', 'tok2', 'X', 'Yes',"
            "  100, 0.50, '[]', ?, 1, ?)",
            (old_iso, now_iso),
        )
        await conn.commit()
        rows = await _find_stale(conn, max_age_hours=48.0)
    cur = rows[0][6]
    assert cur is None


# ============ copy_engine bypass integration ============

@pytest.mark.asyncio
async def test_copy_engine_skips_slippage_check_when_bypass_true():
    """Sanity: copy_engine NÃO chama check_slippage_or_abort quando
    o signal é stale (bypass=True). Posição despencada não trava."""
    from src.executor.copy_engine import CopyEngine
    from unittest.mock import patch

    cfg = MagicMock()
    cfg.enabled = True
    cfg.mode = "paper"
    cfg.paper_perfect_mirror = False
    cfg.optimistic_execution = False
    cfg.copy_buys = True
    cfg.copy_sells = True
    cfg.avoid_resolved_markets = False
    cfg.enforce_market_duration = False
    cfg.min_confidence_score = 0.6
    cfg.min_price = 0.05
    cfg.max_price = 0.95
    cfg.max_positions = 100
    cfg.whale_max_slippage_pct = 0.03
    cfg.max_spread = 0.02
    cfg.max_concurrent_signals = 1
    cfg.sizing_mode = "fixed"
    cfg.fixed_size_usd = 10.0
    cfg.max_position_usd = 100.0
    cfg.max_portfolio_usd = 500.0
    cfg.max_tag_exposure_pct = 1.0
    cfg.max_daily_loss_usd = 100.0
    cfg.max_drawdown_pct = 1.0

    risk_mgr = MagicMock()
    risk_mgr.evaluate = MagicMock(return_value=MagicMock(
        allowed=True, reason="OK", sized_usd=10.0,
    ))

    state_provider = MagicMock(return_value=MagicMock(
        total_portfolio_value=500.0, total_invested=0.0, daily_pnl=0.0,
        current_drawdown=0.0, open_positions=0,
    ))

    import asyncio as _aio
    eng = CopyEngine(
        cfg=cfg, clob=MagicMock(), gamma=MagicMock(),
        risk=risk_mgr, queue=_aio.Queue(),
        state=MagicMock(), risk_state_provider=state_provider,
    )

    # Sinal stale: comprou a 0.50, despencou pra 0.05.
    sig = TradeSignal.model_construct(
        id="stale-1", wallet_address="__stale_cleanup__",
        wallet_score=1.0, condition_id="0xC", token_id="tok",
        side="SELL", size=100.0, price=0.05, usd_value=5.0,
        market_title="X", outcome="Yes",
        market_end_date=None, hours_to_resolution=None,
        detected_at=datetime.now(timezone.utc),
        source="polling", status="pending", skip_reason=None,
        bypass_slippage_check=True,
    )

    # Mock check_slippage_or_abort — se for chamado, falha o test.
    async def _explode(*a, **kw):
        raise AssertionError(
            "BUG: copy_engine chamou check_slippage_or_abort em sinal "
            "com bypass_slippage_check=True. Posição vai ficar presa!"
        )

    with patch("src.executor.copy_engine.check_slippage_or_abort", _explode):
        # _make_decision deve PASSAR sem chamar slippage check.
        result = await eng._make_decision(sig, perfect_mirror=False)
    assert result is not None, "stale signal foi rejeitado quando deveria passar"
    decision, ref_price = result
    assert ref_price == 0.05  # anchor do signal foi usado como ref


@pytest.mark.asyncio
async def test_copy_engine_normal_signal_still_validates_slippage():
    """Regression: sinais normais (bypass=False default) AINDA validam Regra 1."""
    from src.executor.copy_engine import CopyEngine
    from unittest.mock import patch

    cfg = MagicMock()
    cfg.enabled = True
    cfg.mode = "paper"
    cfg.paper_perfect_mirror = False
    cfg.optimistic_execution = False
    cfg.copy_buys = True
    cfg.copy_sells = True
    cfg.avoid_resolved_markets = False
    cfg.enforce_market_duration = False
    cfg.min_confidence_score = 0.6
    cfg.min_price = 0.05
    cfg.max_price = 0.95
    cfg.max_positions = 100
    cfg.whale_max_slippage_pct = 0.03
    cfg.max_spread = 0.02
    cfg.max_concurrent_signals = 1
    cfg.sizing_mode = "fixed"
    cfg.fixed_size_usd = 10.0
    cfg.max_position_usd = 100.0
    cfg.max_portfolio_usd = 500.0
    cfg.max_tag_exposure_pct = 1.0
    cfg.max_daily_loss_usd = 100.0
    cfg.max_drawdown_pct = 1.0

    risk_mgr = MagicMock()
    risk_mgr.evaluate = MagicMock(return_value=MagicMock(
        allowed=True, reason="OK", sized_usd=10.0,
    ))
    state_provider = MagicMock(return_value=MagicMock(
        total_portfolio_value=500.0, total_invested=0.0, daily_pnl=0.0,
        current_drawdown=0.0, open_positions=0,
    ))
    import asyncio as _aio
    eng = CopyEngine(
        cfg=cfg, clob=MagicMock(), gamma=MagicMock(),
        risk=risk_mgr, queue=_aio.Queue(),
        state=MagicMock(), risk_state_provider=state_provider,
    )

    # Sinal NORMAL (bypass=False default). Mock slippage retorna ref.
    sig = TradeSignal.model_construct(
        id="normal-1", wallet_address="0xWHALE",
        wallet_score=0.8, condition_id="0xC", token_id="tok",
        side="BUY", size=100.0, price=0.50, usd_value=50.0,
        market_title="X", outcome="Yes",
        market_end_date=None, hours_to_resolution=24.0,
        detected_at=datetime.now(timezone.utc),
        source="websocket", status="pending", skip_reason=None,
        # bypass_slippage_check default False
    )

    called = {"yes": False}

    async def _slippage_check(*a, **kw):
        called["yes"] = True
        return 0.51  # OK, dentro do tolerance

    with patch("src.executor.copy_engine.check_slippage_or_abort", _slippage_check):
        result = await eng._make_decision(sig, perfect_mirror=False)
    assert called["yes"] is True, "Regra 1 PRECISA validar sinais normais"
    assert result is not None
