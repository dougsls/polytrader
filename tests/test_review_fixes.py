"""Cobertura dos 6 findings de review (production hardening).

Item 1 (P0): SELL respeita signal.size em tokens
Item 2 (P0): submit ≠ fill em live + GTC
Item 3 (P1): persist_trade INSERT OR IGNORE preserva status
Item 4 (P1): PRICE_BAND BUY-only
Item 5 (P1): config flags wired
Item 6 (P2): depth sizing live integrado
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.config import load_yaml_config
from src.core.database import get_connection, init_database
from src.core.models import CopyTrade, RiskState, TradeSignal
from src.core.state import InMemoryState
from src.executor.copy_engine import CopyEngine
from src.executor.order_manager import (
    build_draft,
    persist_trade,
    update_trade_status,
)
from src.executor.risk_manager import RiskManager


def _make_signal(**overrides) -> TradeSignal:
    base = dict(
        id="sig-1", wallet_address="0xWHALE", wallet_score=0.8,
        condition_id="0xCID", token_id="tok-1", side="BUY",
        size=100.0, price=0.50, usd_value=50.0,
        market_title="X", outcome="Yes",
        market_end_date=None, hours_to_resolution=24.0,
        detected_at=datetime.now(timezone.utc),
        source="websocket", status="pending", skip_reason=None,
    )
    base.update(overrides)
    return TradeSignal.model_construct(**base)


def _make_engine_cfg(**overrides: Any) -> Any:
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
    cfg.default_order_type = "GTC"
    cfg.fok_fallback = False
    cfg.fok_fallback_timeout_seconds = 30
    cfg.limit_price_offset = 0.0
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _build_engine(cfg, state: InMemoryState | None = None, **extra) -> CopyEngine:
    state = state or InMemoryState()
    risk_mgr = MagicMock()
    risk_mgr.evaluate = MagicMock(return_value=MagicMock(
        allowed=True, reason="OK", sized_usd=10.0,
    ))
    state_provider = MagicMock(return_value=MagicMock(
        total_portfolio_value=500.0, total_invested=0.0, daily_pnl=0.0,
        current_drawdown=0.0, open_positions=0,
    ))
    return CopyEngine(
        cfg=cfg, clob=MagicMock(), gamma=MagicMock(),
        risk=risk_mgr, queue=asyncio.Queue(),
        state=state, risk_state_provider=state_provider,
        **extra,
    )


# ============ Item 1 — SELL usa signal.size em tokens ============

@pytest.mark.asyncio
async def test_build_draft_sell_uses_signal_size_not_usd_div_price():
    """SELL: raw_size = signal.size (Exit Sync), NÃO sized_usd / price."""
    sig = _make_signal(side="SELL", size=42.0, price=0.50)
    cfg = MagicMock()
    cfg.mode = "paper"
    cfg.paper_perfect_mirror = False
    cfg.limit_price_offset = 0.0
    cfg.paper_apply_fees = 0.0
    cfg.paper_simulate_slippage = 0.0
    gamma = MagicMock()
    gamma.get_market = AsyncMock(return_value={
        "tick_size": 0.01, "neg_risk": False,
    })
    # sized_usd 999 (irrelevante pra SELL — não deve afetar size).
    draft, trade, _spec = await build_draft(
        signal=sig, sized_usd=999.0, ref_price=0.50,
        gamma=gamma, cfg=cfg,
    )
    assert float(draft.size) == 42.0  # signal.size, não 999/0.50=1998


@pytest.mark.asyncio
async def test_build_draft_sell_caps_at_bot_size():
    """sell_size_cap_tokens limita signal.size se bot tem menos do que
    o Exit Sync calculou (drift state vs DB)."""
    sig = _make_signal(side="SELL", size=100.0, price=0.50)
    cfg = MagicMock()
    cfg.mode = "paper"
    cfg.paper_perfect_mirror = False
    cfg.limit_price_offset = 0.0
    cfg.paper_apply_fees = 0.0
    cfg.paper_simulate_slippage = 0.0
    gamma = MagicMock()
    gamma.get_market = AsyncMock(return_value={
        "tick_size": 0.01, "neg_risk": False,
    })
    draft, _trade, _spec = await build_draft(
        signal=sig, sized_usd=999.0, ref_price=0.50,
        gamma=gamma, cfg=cfg,
        sell_size_cap_tokens=30.0,  # bot só tem 30
    )
    assert float(draft.size) == 30.0  # capped


@pytest.mark.asyncio
async def test_build_draft_buy_keeps_usd_based_sizing():
    """BUY: comportamento legado preservado (raw_size = sized_usd / price)."""
    sig = _make_signal(side="BUY", size=999.0, price=0.50)  # signal.size ignorado
    cfg = MagicMock()
    cfg.mode = "paper"
    cfg.paper_perfect_mirror = False
    cfg.limit_price_offset = 0.0
    cfg.paper_apply_fees = 0.0
    cfg.paper_simulate_slippage = 0.0
    gamma = MagicMock()
    gamma.get_market = AsyncMock(return_value={
        "tick_size": 0.01, "neg_risk": False,
    })
    draft, _trade, _spec = await build_draft(
        signal=sig, sized_usd=10.0, ref_price=0.50,
        gamma=gamma, cfg=cfg,
    )
    # 10 USD / 0.50 = 20 tokens, quantizado pro size_step
    assert float(draft.size) == 20.0


# ============ Item 4 — PRICE_BAND BUY-only ============

def test_sell_outside_price_band_passes():
    """SELL com price=0.02 (abaixo de min_price=0.05) DEVE passar."""
    rm = RiskManager(load_yaml_config().executor)
    state = RiskState(
        total_portfolio_value=500.0, total_invested=0.0,
        total_unrealized_pnl=0.0, total_realized_pnl=0.0,
        daily_pnl=0.0, max_drawdown=0.0, current_drawdown=0.0,
        open_positions=0,
    )
    sig = _make_signal(side="SELL", price=0.02)
    d = rm.evaluate(sig, state)
    assert d.allowed
    assert "PRICE_BAND" not in d.reason


def test_buy_outside_price_band_blocked():
    """BUY com price fora da banda continua bloqueado."""
    rm = RiskManager(load_yaml_config().executor)
    state = RiskState(
        total_portfolio_value=500.0, total_invested=0.0,
        total_unrealized_pnl=0.0, total_realized_pnl=0.0,
        daily_pnl=0.0, max_drawdown=0.0, current_drawdown=0.0,
        open_positions=0,
    )
    sig = _make_signal(side="BUY", price=0.02)
    d = rm.evaluate(sig, state)
    assert not d.allowed
    assert "PRICE_BAND" in d.reason


# ============ Item 5 — Config flags ============

@pytest.mark.asyncio
async def test_copy_buys_disabled_blocks_buy():
    cfg = _make_engine_cfg(copy_buys=False)
    eng = _build_engine(cfg)
    sig = _make_signal(side="BUY")
    eng._mark_skipped = AsyncMock()  # type: ignore[method-assign]
    await eng.handle_signal(sig)
    eng._mark_skipped.assert_awaited_once()
    assert "COPY_BUYS_DISABLED" in eng._mark_skipped.call_args[0][1]


@pytest.mark.asyncio
async def test_copy_sells_disabled_blocks_normal_sell():
    cfg = _make_engine_cfg(copy_sells=False)
    eng = _build_engine(cfg)
    sig = _make_signal(side="SELL", bypass_slippage_check=False)
    eng._mark_skipped = AsyncMock()  # type: ignore[method-assign]
    await eng.handle_signal(sig)
    eng._mark_skipped.assert_awaited_once()
    assert "COPY_SELLS_DISABLED" in eng._mark_skipped.call_args[0][1]


@pytest.mark.asyncio
async def test_copy_sells_disabled_allows_emergency_exit():
    """Stale cleanup (bypass_slippage_check=True) BYPASSA copy_sells=False."""
    cfg = _make_engine_cfg(copy_sells=False)
    eng = _build_engine(cfg)
    sig = _make_signal(side="SELL", bypass_slippage_check=True)
    eng._mark_skipped = AsyncMock()  # type: ignore[method-assign]
    # Mock o resto do pipeline pra não crashar
    eng._make_decision = AsyncMock(return_value=None)  # type: ignore[method-assign]
    await eng.handle_signal(sig)
    # _mark_skipped pode ser chamado por outros motivos (state None etc.)
    # mas NÃO com COPY_SELLS_DISABLED.
    if eng._mark_skipped.called:
        reasons = [c.args[1] for c in eng._mark_skipped.call_args_list]
        assert all("COPY_SELLS_DISABLED" not in r for r in reasons)


@pytest.mark.asyncio
async def test_executor_disabled_blocks_all_signals():
    cfg = _make_engine_cfg(enabled=False)
    eng = _build_engine(cfg)
    sig = _make_signal(side="BUY")
    eng._mark_skipped = AsyncMock()  # type: ignore[method-assign]
    await eng.handle_signal(sig)
    eng._mark_skipped.assert_awaited_once()
    assert "EXECUTOR_DISABLED" in eng._mark_skipped.call_args[0][1]


@pytest.mark.asyncio
async def test_avoid_resolved_markets_skips_closed():
    """Mercado closed/resolved → skip MARKET_RESOLVED."""
    cfg = _make_engine_cfg(avoid_resolved_markets=True)
    eng = _build_engine(cfg)
    eng._gamma.get_market = AsyncMock(  # type: ignore[method-assign]
        return_value={"closed": True}
    )
    # Mock _mark_skipped pra evitar UPDATE real em trade_signals (sem
    # tabela em DB de teste isolado). Verificamos que foi chamado com
    # MARKET_RESOLVED.
    eng._mark_skipped = AsyncMock()  # type: ignore[method-assign]
    sig = _make_signal(side="BUY")
    result = await eng._make_decision(sig, perfect_mirror=False)
    assert result is None  # rejected
    eng._mark_skipped.assert_awaited_once()
    assert eng._mark_skipped.call_args.args[1] == "MARKET_RESOLVED"


# ============ Item 3 — INSERT OR IGNORE preserva status ============

async def _seed_signal(db: Path, signal_id: str, wallet: str = "0xa") -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    async with get_connection(db) as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO tracked_wallets"
            " (address, score, tracked_since, updated_at) VALUES (?,?,?,?)",
            (wallet, 0.8, now_iso, now_iso),
        )
        await conn.execute(
            "INSERT INTO trade_signals"
            " (id, wallet_address, wallet_score, condition_id, token_id, side,"
            "  size, price, usd_value, market_title, outcome, detected_at, source)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (signal_id, wallet, 0.8, "0xC", "t", "BUY",
             10.0, 0.5, 5.0, "X", "Yes", now_iso, "polling"),
        )
        await conn.commit()


@pytest.mark.asyncio
async def test_insert_or_ignore_does_not_overwrite_filled_status(tmp_path: Path):
    """⚠️ Item 3 — race INSERT/UPDATE.

    Se persist_trade rodar de novo após o trade já ter sido marcado
    como 'filled', NÃO deve voltar pra 'pending'.
    """
    db = tmp_path / "race.db"
    await init_database(db)
    await _seed_signal(db, "sig-race")

    trade = CopyTrade(
        id="tr-race", signal_id="sig-race", condition_id="c", token_id="t",
        side="BUY", intended_size=10.0, intended_price=0.5,
        status="pending", created_at=datetime.now(timezone.utc),
    )
    await persist_trade(trade, db_path=db)
    # UPDATE pra status final
    await update_trade_status(trade.id, status="filled", db_path=db)
    # Race: persist_trade roda de novo
    trade.status = "pending"
    await persist_trade(trade, db_path=db)

    async with get_connection(db) as conn:
        async with conn.execute(
            "SELECT status FROM copy_trades WHERE id=?", ("tr-race",)
        ) as cur:
            row = await cur.fetchone()
    assert row[0] == "filled", "Status final foi destruído por re-INSERT"


@pytest.mark.asyncio
async def test_update_trade_status_persists_order_id(tmp_path: Path):
    """update_trade_status grava order_id pra correlacionar fills."""
    db = tmp_path / "sub.db"
    await init_database(db)
    await _seed_signal(db, "sig-sub")
    trade = CopyTrade(
        id="tr-sub", signal_id="sig-sub", condition_id="c", token_id="t",
        side="BUY", intended_size=10.0, intended_price=0.5,
        status="pending", created_at=datetime.now(timezone.utc),
    )
    await persist_trade(trade, db_path=db)
    await update_trade_status(
        trade.id, status="submitted", order_id="0xABC", db_path=db,
    )
    async with get_connection(db) as conn:
        async with conn.execute(
            "SELECT status, order_id FROM copy_trades WHERE id=?", ("tr-sub",)
        ) as cur:
            row = await cur.fetchone()
    assert row[0] == "submitted"
    assert row[1] == "0xABC"


# ============ Item 2 — submit ≠ fill ============

@pytest.mark.asyncio
async def test_handle_fill_event_unknown_order_returns_false():
    """Fill event para order_id não-pendente é safely ignored."""
    cfg = _make_engine_cfg()
    eng = _build_engine(cfg)
    ok = await eng.handle_fill_event(
        order_id="ghost", executed_size=10.0, executed_price=0.5,
    )
    assert ok is False


@pytest.mark.asyncio
async def test_handle_fill_event_consumes_pending_full_fill(tmp_path: Path):
    """Fill final remove o trade do _pending_fills e aplica."""
    db = tmp_path / "fe.db"
    await init_database(db)
    await _seed_signal(db, "sig-fe")
    cfg = _make_engine_cfg(mode="paper")
    state = InMemoryState()
    eng = _build_engine(cfg, state=state, db_path=db)
    # Cria trade em pending_fills manualmente (simulando GTC submetido).
    sig = _make_signal(id="sig-fe", side="BUY", size=10.0, price=0.5)
    trade = CopyTrade(
        id="tr-fe", signal_id="sig-fe", condition_id="0xCID", token_id="tok-1",
        side="BUY", intended_size=10.0, intended_price=0.5,
        status="submitted", created_at=datetime.now(timezone.utc),
    )
    await persist_trade(trade, db_path=db)
    eng._pending_fills["order-fe"] = (sig, trade)
    eng._db_path = db
    eng._conn = None  # força usar db_path

    ok = await eng.handle_fill_event(
        order_id="order-fe", executed_size=10.0, executed_price=0.5,
    )
    assert ok is True
    # Removido do mapa após fill final
    assert "order-fe" not in eng._pending_fills
    # Estado RAM atualizado
    assert state.bot_size("tok-1") == 10.0


# ============ Item 6 — Depth sizing live ============

@pytest.mark.asyncio
async def test_depth_sizing_disabled_passes_through():
    """depth_cfg=None → sem ajuste, sem skip."""
    cfg = _make_engine_cfg()
    eng = _build_engine(cfg, depth_cfg=None)
    decision = MagicMock()
    decision.sized_usd = 50.0
    sig = _make_signal(side="BUY")
    ok, reason = await eng._apply_depth_sizing(sig, decision)
    assert ok is True
    assert reason is None
    assert decision.sized_usd == 50.0


@pytest.mark.asyncio
async def test_depth_sizing_caps_buy_to_fillable():
    """BUY com fillable_size_usd < target → corta sized_usd."""
    cfg = _make_engine_cfg()
    depth_cfg = MagicMock()
    depth_cfg.enabled = True
    depth_cfg.max_impact_pct = 0.02
    depth_cfg.max_levels = 5
    eng = _build_engine(cfg, depth_cfg=depth_cfg)
    # Book com fillable só $20 (no impact cap), target $50 → corta pra 20
    eng._clob.book = AsyncMock(return_value={  # type: ignore[method-assign]
        "asks": [{"price": "0.50", "size": "40"}],  # fill cap = 20 USD
    })
    decision = MagicMock()
    decision.sized_usd = 50.0
    sig = _make_signal(side="BUY")
    ok, reason = await eng._apply_depth_sizing(sig, decision)
    assert ok is True
    assert reason is None
    assert decision.sized_usd == 20.0


@pytest.mark.asyncio
async def test_depth_sizing_skips_on_thin_book():
    """fillable_size_usd < 1.0 → DEPTH_TOO_THIN."""
    cfg = _make_engine_cfg()
    depth_cfg = MagicMock()
    depth_cfg.enabled = True
    depth_cfg.max_impact_pct = 0.02
    depth_cfg.max_levels = 5
    eng = _build_engine(cfg, depth_cfg=depth_cfg)
    eng._clob.book = AsyncMock(return_value={"asks": []})  # type: ignore[method-assign]
    decision = MagicMock()
    decision.sized_usd = 50.0
    sig = _make_signal(side="BUY")
    ok, reason = await eng._apply_depth_sizing(sig, decision)
    assert ok is False
    assert reason == "DEPTH_TOO_THIN"


@pytest.mark.asyncio
async def test_depth_sizing_bypassed_for_emergency_exit():
    """bypass_slippage_check=True (stale cleanup) ignora depth gate —
    capital travado é pior que aceitar impact."""
    cfg = _make_engine_cfg()
    depth_cfg = MagicMock()
    depth_cfg.enabled = True
    eng = _build_engine(cfg, depth_cfg=depth_cfg)
    decision = MagicMock()
    decision.sized_usd = 50.0
    # Mesmo com book vazio, stale cleanup passa.
    sig = _make_signal(side="SELL", bypass_slippage_check=True)
    ok, reason = await eng._apply_depth_sizing(sig, decision)
    assert ok is True
    assert reason is None
