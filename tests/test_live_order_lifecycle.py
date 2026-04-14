from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.config import load_yaml_config
from src.core.database import get_connection, init_database
from src.core.models import RiskState, TradeSignal
from src.core.state import InMemoryState
from src.executor.copy_engine import CopyEngine
from src.executor.risk_manager import RiskManager


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _insert_signal(db: Path, signal: TradeSignal) -> None:
    async with get_connection(db) as conn:
        await conn.execute(
            """
            INSERT OR REPLACE INTO tracked_wallets
                (address, score, tracked_since, is_active, updated_at)
            VALUES (?, ?, ?, 1, ?)
            """,
            (
                signal.wallet_address,
                signal.wallet_score,
                signal.detected_at.isoformat(),
                signal.detected_at.isoformat(),
            ),
        )
        await conn.execute(
            """
            INSERT INTO trade_signals
                (id, wallet_address, wallet_score, condition_id, token_id, side,
                 size, price, usd_value, market_title, outcome, market_end_date,
                 hours_to_resolution, detected_at, source, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal.id,
                signal.wallet_address,
                signal.wallet_score,
                signal.condition_id,
                signal.token_id,
                signal.side,
                signal.size,
                signal.price,
                signal.usd_value,
                signal.market_title,
                signal.outcome,
                signal.market_end_date.isoformat() if signal.market_end_date else None,
                signal.hours_to_resolution,
                signal.detected_at.isoformat(),
                signal.source,
                signal.status,
            ),
        )
        await conn.commit()


async def _seed_open_position(db: Path, *, token_id: str, size: float, avg_entry_price: float) -> None:
    now = _iso_now()
    async with get_connection(db) as conn:
        await conn.execute(
            """
            INSERT INTO bot_positions
                (condition_id, token_id, market_title, outcome, size,
                 avg_entry_price, current_price, source_wallets_json,
                 opened_at, is_open, updated_at)
            VALUES ('cond-1', ?, 'Market', 'Yes', ?, ?, ?, '[]', ?, 1, ?)
            """,
            (token_id, size, avg_entry_price, avg_entry_price, now, now),
        )
        await conn.commit()


async def _fetch_one(db: Path, query: str, params: tuple = ()) -> tuple:
    async with get_connection(db) as conn:
        async with conn.execute(query, params) as cur:
            row = await cur.fetchone()
    assert row is not None
    return tuple(row)


async def _insert_copy_trade(
    db: Path,
    *,
    trade_id: str,
    signal_id: str,
    token_id: str = "token-1",
    side: str = "BUY",
) -> None:
    now = _iso_now()
    async with get_connection(db) as conn:
        await conn.execute(
            """
            INSERT INTO copy_trades
                (id, signal_id, condition_id, token_id, side, intended_size,
                 intended_price, status, created_at)
            VALUES (?, ?, 'cond-1', ?, ?, 10.0, 0.55, 'pending', ?)
            """,
            (trade_id, signal_id, token_id, side, now),
        )
        await conn.commit()


def _executor_cfg(*, fok_fallback: bool = False):
    base = load_yaml_config().executor
    return base.model_copy(
        update={
            "mode": "live",
            "fok_fallback": fok_fallback,
            "fok_fallback_timeout_seconds": 1,
            "max_spread": 0.05,
            "min_confidence_score": 0.1,
            "max_portfolio_usd": 10_000.0,
            "max_position_usd": 2_000.0,
            "fixed_size_usd": 100.0,
            "sizing_mode": "fixed",
        }
    )


def _risk_state() -> RiskState:
    return RiskState(
        total_portfolio_value=10_000.0,
        total_invested=0.0,
        total_unrealized_pnl=0.0,
        total_realized_pnl=0.0,
        daily_pnl=0.0,
        max_drawdown=0.0,
        current_drawdown=0.0,
        open_positions=0,
        is_halted=False,
        halt_reason=None,
    )


def _signal(*, signal_id: str, side: str, token_id: str = "token-1") -> TradeSignal:
    now = datetime.now(timezone.utc)
    return TradeSignal(
        id=signal_id,
        wallet_address="0xwhale",
        wallet_score=0.9,
        condition_id="cond-1",
        token_id=token_id,
        side=side,
        size=10.0,
        price=0.55,
        usd_value=5.5,
        market_title="Market",
        outcome="Yes",
        market_end_date=now,
        hours_to_resolution=1.0,
        detected_at=now,
        source="polling",
        status="pending",
    )


@pytest.mark.asyncio
async def test_live_partial_then_filled_updates_position_and_realized_pnl(tmp_path: Path) -> None:
    from src.executor.live_orders import LiveOrderTracker

    db = tmp_path / "lifecycle.db"
    await init_database(db)
    await _seed_open_position(db, token_id="token-1", size=10.0, avg_entry_price=0.4)

    state = InMemoryState()
    await state.reload_from_db(db_path=db)

    signal = _signal(signal_id="sig-sell", side="SELL")
    await _insert_signal(db, signal)

    gamma = AsyncMock()
    gamma.get_market = AsyncMock(return_value={"tick_size": 0.01, "neg_risk": False})

    clob = AsyncMock()
    clob.book = AsyncMock(return_value={"bids": [{"price": 0.55}], "asks": [{"price": 0.56}]})
    clob.post_order = AsyncMock(return_value={"success": True, "orderID": "ord-1"})

    tracker = LiveOrderTracker(conn=None, state=state, db_path=db)
    cfg = _executor_cfg()
    engine = CopyEngine(
        cfg=cfg,
        clob=clob,
        gamma=gamma,
        risk=RiskManager(cfg),
        queue=AsyncMock(),
        state=state,
        risk_state_provider=_risk_state,
        db_path=db,
        live_order_tracker=tracker,
    )

    await engine.handle_signal(signal)

    trade_row = await _fetch_one(
        db,
        "SELECT status, order_id, submitted_at, executed_size FROM copy_trades",
    )
    assert trade_row[0] == "submitted"
    assert trade_row[1] == "ord-1"
    assert trade_row[2] is not None
    assert trade_row[3] is None
    assert state.bot_size("token-1") == 10.0

    await tracker.handle_user_message(
        {
            "event_type": "trade",
            "id": "fill-1",
            "order_id": "ord-1",
            "size_matched": 4.0,
            "price": 0.5,
            "timestamp": _iso_now(),
        }
    )
    await tracker.handle_user_message(
        {
            "event_type": "order",
            "order_id": "ord-1",
            "status": "live",
            "size_matched": 4.0,
            "avg_price": 0.5,
            "timestamp": _iso_now(),
        }
    )

    partial_position = await _fetch_one(
        db,
        "SELECT size, realized_pnl, is_open FROM bot_positions WHERE token_id='token-1'",
    )
    assert partial_position == (6.0, pytest.approx(0.4), 1)
    assert state.bot_size("token-1") == 6.0

    await tracker.handle_user_message(
        {
            "event_type": "trade",
            "id": "fill-2",
            "order_id": "ord-1",
            "size_matched": 6.0,
            "price": 0.6,
            "timestamp": _iso_now(),
        }
    )
    await tracker.handle_user_message(
        {
            "event_type": "order",
            "order_id": "ord-1",
            "status": "filled",
            "size_matched": 10.0,
            "avg_price": 0.56,
            "timestamp": _iso_now(),
        }
    )

    trade_final = await _fetch_one(
        db,
        "SELECT status, executed_size, executed_price FROM copy_trades",
    )
    assert trade_final == ("filled", 10.0, pytest.approx(0.56))

    position_final = await _fetch_one(
        db,
        "SELECT size, realized_pnl, is_open, close_reason FROM bot_positions WHERE token_id='token-1'",
    )
    assert position_final == (0.0, pytest.approx(1.6), 0, "sold")
    assert state.bot_size("token-1") == 0.0


@pytest.mark.asyncio
async def test_live_submitted_then_cancelled_creates_no_phantom_position(tmp_path: Path) -> None:
    from src.executor.live_orders import LiveOrderTracker

    db = tmp_path / "cancelled.db"
    await init_database(db)

    state = InMemoryState()
    signal = _signal(signal_id="sig-buy", side="BUY")
    await _insert_signal(db, signal)

    gamma = AsyncMock()
    gamma.get_market = AsyncMock(return_value={"tick_size": 0.01, "neg_risk": False})

    clob = AsyncMock()
    clob.book = AsyncMock(return_value={"bids": [{"price": 0.54}], "asks": [{"price": 0.55}]})
    clob.post_order = AsyncMock(return_value={"success": True, "orderID": "ord-cancel"})

    tracker = LiveOrderTracker(conn=None, state=state, db_path=db)
    cfg = _executor_cfg()
    engine = CopyEngine(
        cfg=cfg,
        clob=clob,
        gamma=gamma,
        risk=RiskManager(cfg),
        queue=AsyncMock(),
        state=state,
        risk_state_provider=_risk_state,
        db_path=db,
        live_order_tracker=tracker,
    )

    await engine.handle_signal(signal)
    await tracker.handle_user_message(
        {
            "event_type": "order",
            "order_id": "ord-cancel",
            "status": "cancelled",
            "size_matched": 0.0,
            "timestamp": _iso_now(),
        }
    )

    trade_row = await _fetch_one(db, "SELECT status, executed_size FROM copy_trades")
    assert trade_row == ("cancelled", None)
    async with get_connection(db) as conn:
        async with conn.execute("SELECT COUNT(*) FROM bot_positions WHERE is_open=1") as cur:
            count = (await cur.fetchone())[0]
    assert count == 0
    assert state.bot_size("token-1") == 0.0


@pytest.mark.asyncio
async def test_live_submitted_then_rejected_creates_no_phantom_position(tmp_path: Path) -> None:
    from src.executor.live_orders import LiveOrderTracker

    db = tmp_path / "rejected.db"
    await init_database(db)

    state = InMemoryState()
    signal = _signal(signal_id="sig-reject", side="BUY")
    await _insert_signal(db, signal)

    gamma = AsyncMock()
    gamma.get_market = AsyncMock(return_value={"tick_size": 0.01, "neg_risk": False})

    clob = AsyncMock()
    clob.book = AsyncMock(return_value={"bids": [{"price": 0.54}], "asks": [{"price": 0.55}]})
    clob.post_order = AsyncMock(return_value={"success": True, "orderID": "ord-reject"})

    tracker = LiveOrderTracker(conn=None, state=state, db_path=db)
    cfg = _executor_cfg()
    engine = CopyEngine(
        cfg=cfg,
        clob=clob,
        gamma=gamma,
        risk=RiskManager(cfg),
        queue=AsyncMock(),
        state=state,
        risk_state_provider=_risk_state,
        db_path=db,
        live_order_tracker=tracker,
    )

    await engine.handle_signal(signal)
    await tracker.handle_user_message(
        {
            "event_type": "order",
            "order_id": "ord-reject",
            "status": "rejected",
            "size_matched": 0.0,
            "error": "balance",
            "timestamp": _iso_now(),
        }
    )

    trade_row = await _fetch_one(db, "SELECT status, error FROM copy_trades")
    assert trade_row == ("rejected", "balance")
    assert state.bot_size("token-1") == 0.0


@pytest.mark.asyncio
async def test_watchdog_fallback_replaces_open_order_and_accepts_fill_on_new_order(tmp_path: Path) -> None:
    from src.executor.live_orders import LiveOrderTracker
    from src.executor.order_watchdog import watchdog_order

    db = tmp_path / "watchdog.db"
    await init_database(db)

    state = InMemoryState()
    signal = _signal(signal_id="sig-watchdog", side="BUY")
    await _insert_signal(db, signal)
    await _insert_copy_trade(db, trade_id="trade-1", signal_id=signal.id, side="BUY")

    tracker = LiveOrderTracker(conn=None, state=state, db_path=db)
    await tracker.record_submitted_order(
        trade_id="trade-1",
        signal_id=signal.id,
        condition_id=signal.condition_id,
        order_id="ord-old",
    )

    draft = MagicMock()
    draft.neg_risk = False
    draft.tick_size = "0.01"
    draft.token_id = signal.token_id
    draft.side = "BUY"
    draft.price = 0.55
    draft.size = 10.0

    clob = AsyncMock()
    clob.get_order = AsyncMock(return_value={"status": "live"})
    clob.cancel_order = AsyncMock(return_value={"success": True})
    clob.post_order = AsyncMock(return_value={"success": True, "orderID": "ord-new"})

    await watchdog_order(
        clob=clob,
        order_id="ord-old",
        draft=draft,
        timeout_s=0,
        poll_interval_s=0,
        on_reposted=lambda order_id: tracker.replace_active_order(
            trade_id="trade-1",
            condition_id=signal.condition_id,
            order_id=order_id,
        ),
    )

    await tracker.handle_user_message(
        {
            "event_type": "trade",
            "id": "fill-watchdog",
            "order_id": "ord-new",
            "size_matched": 10.0,
            "price": 0.55,
            "timestamp": _iso_now(),
        }
    )
    await tracker.handle_user_message(
        {
            "event_type": "order",
            "order_id": "ord-new",
            "status": "filled",
            "size_matched": 10.0,
            "avg_price": 0.55,
            "timestamp": _iso_now(),
        }
    )

    trade_row = await _fetch_one(db, "SELECT order_id, status, executed_size FROM copy_trades WHERE id='trade-1'")
    assert trade_row == ("ord-new", "filled", 10.0)


@pytest.mark.asyncio
async def test_restart_recovery_reconciles_open_submitted_order(tmp_path: Path) -> None:
    from src.executor.live_orders import LiveOrderTracker

    db = tmp_path / "recovery.db"
    await init_database(db)
    await _seed_open_position(db, token_id="token-1", size=10.0, avg_entry_price=0.4)

    signal = _signal(signal_id="sig-recovery", side="SELL")
    await _insert_signal(db, signal)
    await _insert_copy_trade(db, trade_id="trade-recovery", signal_id=signal.id, side="SELL")

    first_state = InMemoryState()
    await first_state.reload_from_db(db_path=db)
    tracker = LiveOrderTracker(conn=None, state=first_state, db_path=db)
    await tracker.record_submitted_order(
        trade_id="trade-recovery",
        signal_id=signal.id,
        condition_id=signal.condition_id,
        order_id="ord-recovery",
    )
    await tracker.handle_user_message(
        {
            "event_type": "trade",
            "id": "fill-before-restart",
            "order_id": "ord-recovery",
            "size_matched": 4.0,
            "price": 0.5,
            "timestamp": _iso_now(),
        }
    )
    await tracker.handle_user_message(
        {
            "event_type": "order",
            "order_id": "ord-recovery",
            "status": "live",
            "size_matched": 4.0,
            "avg_price": 0.5,
            "timestamp": _iso_now(),
        }
    )

    restarted_state = InMemoryState()
    await restarted_state.reload_from_db(db_path=db)

    clob = AsyncMock()
    clob.get_order = AsyncMock(
        return_value={
            "orderID": "ord-recovery",
            "status": "filled",
            "size_matched": 10.0,
            "avg_price": 0.56,
            "timestamp": _iso_now(),
        }
    )

    restarted_tracker = LiveOrderTracker(conn=None, state=restarted_state, db_path=db)
    await restarted_tracker.reconcile_open_orders(clob=clob)

    trade_row = await _fetch_one(
        db,
        "SELECT status, executed_size, executed_price FROM copy_trades WHERE id='trade-recovery'",
    )
    assert trade_row == ("filled", 10.0, pytest.approx(0.56))

    position_row = await _fetch_one(
        db,
        "SELECT size, realized_pnl, is_open FROM bot_positions WHERE token_id='token-1'",
    )
    assert position_row == (0.0, pytest.approx(1.6), 0)
    assert restarted_state.bot_size("token-1") == 0.0
