"""Cobertura dos 2 riscos funcionais do review.

1. UserWSClient.add_market — auto-subscribe quando ordem em condition_id novo
2. _run_watchdog_and_handle_fill — FOK fallback aplica fill local
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.auth import L2Credentials
from src.api.user_ws_client import UserWSClient
from src.core.database import get_connection, init_database
from src.core.models import CopyTrade, TradeSignal
from src.core.state import InMemoryState
from src.executor.copy_engine import CopyEngine
from src.executor.order_manager import persist_trade


def _make_signal(**overrides: Any) -> TradeSignal:
    base = dict(
        id="sig-x", wallet_address="0xWHALE", wallet_score=0.8,
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
    cfg.fok_fallback = True
    cfg.fok_fallback_timeout_seconds = 30
    cfg.limit_price_offset = 0.0
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


# ============ 1. UserWSClient.add_market ============

@pytest.mark.asyncio
async def test_add_market_appends_to_set():
    """add_market adiciona ao set vivo."""
    creds = L2Credentials(
        api_key="k", secret="s", passphrase="p", address="0xa",
    )
    cids: set[str] = set()
    client = UserWSClient(
        credentials=creds, on_fill=AsyncMock(), condition_ids=cids,
    )
    added = await client.add_market("0xCONDITION_NEW")
    assert added is True
    assert "0xcondition_new" in cids


@pytest.mark.asyncio
async def test_add_market_idempotent():
    """Adicionar mesmo cid duas vezes retorna False na segunda."""
    creds = L2Credentials(api_key="k", secret="s", passphrase="p", address="0xa")
    cids: set[str] = set()
    client = UserWSClient(credentials=creds, on_fill=AsyncMock(), condition_ids=cids)
    assert await client.add_market("0xC") is True
    assert await client.add_market("0xC") is False
    assert len(cids) == 1


@pytest.mark.asyncio
async def test_add_market_resubscribes_when_ws_connected():
    """Se WS está conectado, add_market dispara nova subscription."""
    creds = L2Credentials(
        api_key="k", secret="s", passphrase="p", address="0xa",
    )
    client = UserWSClient(
        credentials=creds, on_fill=AsyncMock(), condition_ids=set(),
    )
    fake_ws = MagicMock()
    fake_ws.send = AsyncMock()
    client._ws = fake_ws
    await client.add_market("0xNEW_MKT")
    fake_ws.send.assert_awaited_once()
    sent_payload = fake_ws.send.call_args.args[0]
    # JSON contém o markets atualizado
    import orjson
    msg = orjson.loads(sent_payload)
    assert "0xnew_mkt" in msg["markets"]


@pytest.mark.asyncio
async def test_add_market_silently_succeeds_when_ws_disconnected():
    """WS não conectado → adiciona ao set mas não tenta send (sem erro)."""
    creds = L2Credentials(api_key="k", secret="s", passphrase="p", address="0xa")
    client = UserWSClient(credentials=creds, on_fill=AsyncMock(), condition_ids=set())
    # _ws = None; add_market deve só adicionar ao set
    added = await client.add_market("0xPENDING")
    assert added is True
    assert "0xpending" in client._condition_ids


# ============ 2. CopyEngine on_order_submitted callback ============

def _build_engine(state: InMemoryState | None = None, **extra: Any) -> CopyEngine:
    cfg = _make_engine_cfg(mode="live")
    state = state or InMemoryState()
    risk_mgr = MagicMock()
    risk_mgr.evaluate = MagicMock(return_value=MagicMock(
        allowed=True, reason="OK", sized_usd=10.0,
    ))
    risk_mgr.record_post_success = MagicMock()
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


@pytest.mark.asyncio
async def test_engine_calls_on_order_submitted_for_gtc_live(tmp_path: Path):
    """⚠️ Auto-subscribe — GTC live submit dispara o callback com cid."""
    db = tmp_path / "as.db"
    await init_database(db)
    # Seed signal pra FK
    now_iso = datetime.now(timezone.utc).isoformat()
    async with get_connection(db) as conn:
        await conn.execute(
            "INSERT INTO tracked_wallets (address, score, tracked_since, updated_at)"
            " VALUES (?,?,?,?)", ("0xwhale", 0.8, now_iso, now_iso),
        )
        await conn.execute(
            "INSERT INTO trade_signals"
            " (id, wallet_address, wallet_score, condition_id, token_id, side,"
            "  size, price, usd_value, market_title, outcome, detected_at, source)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("sig-x", "0xwhale", 0.8, "0xMKT_NEW", "tok", "BUY",
             10.0, 0.5, 5.0, "X?", "Yes", now_iso, "websocket"),
        )
        await conn.commit()

    captured_cids: list[str] = []

    async def on_submit(cid: str) -> None:
        captured_cids.append(cid)

    eng = _build_engine(on_order_submitted=on_submit)
    eng._db_path = db
    eng._cfg.mode = "live"
    eng._cfg.default_order_type = "GTC"
    # Mock CLOB post_order pra retornar order_id sem fill imediato
    eng._clob.post_order = AsyncMock(return_value={  # type: ignore[method-assign]
        "success": True, "orderID": "0xORDER_GTC", "errorMsg": "",
    })
    # Mock gamma pra build_draft
    eng._gamma.get_market = AsyncMock(return_value={  # type: ignore[method-assign]
        "tick_size": 0.01, "neg_risk": False, "tokens": [],
    })

    sig = _make_signal(id="sig-x", condition_id="0xMKT_NEW", token_id="tok")
    decision = MagicMock()
    decision.sized_usd = 10.0
    await eng._submit_and_apply(
        signal=sig, decision=decision, ref_price=0.50,
        perfect_mirror=False, start_perf=0.0,
    )
    # Callback foi chamado com o condition_id da ordem submetida
    assert captured_cids == ["0xMKT_NEW"]


# ============ 3. Watchdog return — FOK fallback aplica fill ============

@pytest.mark.asyncio
async def test_watchdog_fok_fallback_applies_fill_locally(tmp_path: Path):
    """⚠️ Watchdog wrapper — quando FOK fallback retorna match, fill é
    aplicado localmente (state RAM + DB updated)."""
    db = tmp_path / "wd.db"
    await init_database(db)
    now_iso = datetime.now(timezone.utc).isoformat()
    async with get_connection(db) as conn:
        await conn.execute(
            "INSERT INTO tracked_wallets (address, score, tracked_since, updated_at)"
            " VALUES (?,?,?,?)", ("0xwhale", 0.8, now_iso, now_iso),
        )
        await conn.execute(
            "INSERT INTO trade_signals"
            " (id, wallet_address, wallet_score, condition_id, token_id, side,"
            "  size, price, usd_value, market_title, outcome, detected_at, source)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("sig-w", "0xwhale", 0.8, "0xC", "tok-w", "BUY",
             10.0, 0.5, 5.0, "X?", "Yes", now_iso, "websocket"),
        )
        await conn.commit()

    state = InMemoryState()
    eng = _build_engine(state=state)
    eng._db_path = db

    sig = _make_signal(id="sig-w", condition_id="0xC", token_id="tok-w")
    trade = CopyTrade(
        id="tr-w", signal_id="sig-w", condition_id="0xC", token_id="tok-w",
        side="BUY", intended_size=20.0, intended_price=0.5,
        status="submitted", created_at=datetime.now(timezone.utc),
    )
    await persist_trade(trade, db_path=db)

    # Mock draft + watchdog_order pra simular FOK fallback success
    draft = MagicMock()
    draft.size = 20.0
    draft.price = 0.5

    from unittest.mock import patch

    async def fake_watchdog(*args: Any, **kwargs: Any) -> dict[str, Any]:
        # Simula que FOK fallback preencheu
        return {"success": True, "orderID": "0xORDER_FOK", "errorMsg": ""}

    eng._pending_fills["0xORIG"] = (sig, trade)

    with patch(
        "src.executor.copy_engine.watchdog_order",
        side_effect=fake_watchdog,
    ):
        await eng._run_watchdog_and_handle_fill(
            signal=sig, trade=trade, draft=draft, order_id="0xORIG",
        )

    # Pending entry consumida
    assert "0xORIG" not in eng._pending_fills
    # state RAM atualizado (BUY +20 tokens)
    assert state.bot_size("tok-w") == 20.0
    # DB: copy_trades.status='filled'
    async with get_connection(db) as conn:
        async with conn.execute(
            "SELECT status, executed_size, executed_price FROM copy_trades WHERE id=?",
            ("tr-w",),
        ) as cur:
            row = await cur.fetchone()
    assert row is not None
    assert row[0] == "filled"
    assert row[1] == 20.0
    assert row[2] == 0.5


@pytest.mark.asyncio
async def test_watchdog_returns_none_does_not_apply_fill(tmp_path: Path):
    """Watchdog retorna None (GTC original encheu OU fallback falhou):
    NÃO aplica fill duplicado."""
    db = tmp_path / "wd2.db"
    await init_database(db)
    state = InMemoryState()
    eng = _build_engine(state=state)
    eng._db_path = db

    sig = _make_signal(id="sig-x", condition_id="0xC", token_id="tok-y")
    trade = CopyTrade(
        id="tr-y", signal_id="sig-x", condition_id="0xC", token_id="tok-y",
        side="BUY", intended_size=20.0, intended_price=0.5,
        status="submitted", created_at=datetime.now(timezone.utc),
    )
    draft = MagicMock()
    draft.size = 20.0
    draft.price = 0.5

    from unittest.mock import patch

    async def fake_watchdog_none(*args: Any, **kwargs: Any) -> None:
        return None  # GTC original encheu OU fallback falhou

    eng._pending_fills["0xORIG2"] = (sig, trade)

    with patch(
        "src.executor.copy_engine.watchdog_order",
        side_effect=fake_watchdog_none,
    ):
        await eng._run_watchdog_and_handle_fill(
            signal=sig, trade=trade, draft=draft, order_id="0xORIG2",
        )

    # state RAM NÃO foi tocado — fill esperado vir via UserWS quando
    # GTC original preencher. Sem isso teria duplicate fill.
    assert state.bot_size("tok-y") == 0.0


@pytest.mark.asyncio
async def test_watchdog_unsuccessful_response_does_not_apply():
    """Response do FOK fallback com success=False NÃO aplica fill."""
    state = InMemoryState()
    eng = _build_engine(state=state)
    sig = _make_signal(token_id="tok-z")
    trade = CopyTrade(
        id="tr-z", signal_id="sig-z", condition_id="0xC", token_id="tok-z",
        side="BUY", intended_size=20.0, intended_price=0.5,
        status="submitted", created_at=datetime.now(timezone.utc),
    )
    draft = MagicMock()
    draft.size = 20.0
    draft.price = 0.5

    from unittest.mock import patch

    async def fake_watchdog_fail(*args: Any, **kwargs: Any) -> dict[str, Any]:
        # FOK matched no enchimento? Não — errorMsg presente
        return {"success": False, "errorMsg": "insufficient liquidity"}

    eng._pending_fills["0xFAILED"] = (sig, trade)

    with patch(
        "src.executor.copy_engine.watchdog_order",
        side_effect=fake_watchdog_fail,
    ):
        await eng._run_watchdog_and_handle_fill(
            signal=sig, trade=trade, draft=draft, order_id="0xFAILED",
        )
    # Nada aplicado em state
    assert state.bot_size("tok-z") == 0.0
