"""6 correções finais de produção — testes integrados.

Cobre:
  Item 1: peak tracking → drawdown correto (peak-to-trough)
  Item 2: daily_pnl = realized_24h + unrealized
  Item 3: telegram parse_mode HTML + html.escape sanitize
  Item 4: open_shared_connection row_factory aiosqlite.Row
  Item 5: InMemoryState.whale_add + tracker inline update
  Item 6: metrics.halted gauge atualizado em halt/resume
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from src.core import metrics
from src.core.database import (
    get_connection,
    init_database,
    open_shared_connection,
)
from src.core.state import InMemoryState
from src.executor.balance_cache import BalanceCache
from src.executor.risk_manager import RiskManager
from src.executor.risk_state import build_risk_state


# ============ Item 1 — Peak tracking + drawdown correto ============

@pytest.mark.asyncio
async def test_peak_updates_when_portfolio_goes_up():
    """Peak deve refletir o MAIOR valor já visto."""
    async def fetch_balance() -> float:
        return 1.0  # >0 → is_fresh=True após refresh

    bc = BalanceCache(fetch_balance)
    await bc.refresh_once()
    assert bc.peak_portfolio_value == 0.0

    bc.note_portfolio_value(100.0)
    assert bc.peak_portfolio_value == 100.0
    bc.note_portfolio_value(150.0)
    assert bc.peak_portfolio_value == 150.0
    bc.note_portfolio_value(120.0)  # caiu
    assert bc.peak_portfolio_value == 150.0  # peak inalterado


@pytest.mark.asyncio
async def test_peak_ignores_when_cache_stale():
    """Quando is_fresh=False, peak NÃO atualiza (defesa contra outlier)."""
    async def fetch_balance() -> float:
        return 100.0

    bc = BalanceCache(fetch_balance)
    # Sem refresh → is_fresh=False → noting peak é no-op
    bc.note_portfolio_value(999.0)
    assert bc.peak_portfolio_value == 0.0


@pytest.mark.asyncio
async def test_peak_bootstrap_survives_restart():
    """bootstrap_peak inicializa o peak (simula leitura de risk_snapshots)."""
    async def fetch_balance() -> float:
        return 1.0

    bc = BalanceCache(fetch_balance)
    bc.bootstrap_peak(500.0)
    assert bc.peak_portfolio_value == 500.0
    # Bootstrap menor não regride o peak (defensive)
    bc.bootstrap_peak(300.0)
    assert bc.peak_portfolio_value == 500.0


@pytest.mark.asyncio
async def test_drawdown_uses_peak_to_trough(tmp_path: Path):
    """Drawdown = (peak - current) / peak — não loss vs invested."""
    db = tmp_path / "rs.db"
    await init_database(db)
    now_iso = datetime.now(timezone.utc).isoformat()
    async with get_connection(db) as conn:
        # Position aberta — invested $100, current $80 (loss aparente 20%)
        await conn.execute(
            "INSERT INTO bot_positions"
            " (id, condition_id, token_id, market_title, outcome,"
            "  size, avg_entry_price, current_price, source_wallets_json,"
            "  opened_at, is_open, updated_at)"
            " VALUES (1, '0xC', 'tok', 'm', 'Yes',"
            "  100.0, 1.0, 0.80, '[]', ?, 1, ?)",
            (now_iso, now_iso),
        )
        await conn.commit()

    async def fetch_balance() -> float:
        return 50.0  # cash livre

    bc = BalanceCache(fetch_balance)
    await bc.refresh_once()
    # Bootstrap peak num valor histórico maior — simula dia bom no passado
    bc.bootstrap_peak(200.0)

    async with get_connection(db) as conn:
        rs = await build_risk_state(balance_cache=bc, conn=conn)

    # current portfolio = usdc(50) + current_value(80) = 130
    # peak histórico = 200 (bootstrapped)
    # DD = (200 - 130) / 200 = 0.35 = 35%
    assert rs.current_drawdown == pytest.approx(0.35)
    assert rs.total_portfolio_value == pytest.approx(130.0)


# ============ Item 2 — Daily PnL = realized_24h + unrealized ============

@pytest.mark.asyncio
async def test_daily_pnl_includes_realized_24h_plus_unrealized(tmp_path: Path):
    """daily_pnl deve refletir perdas realizadas + unrealized atual.
    Regression: antes era só `unrealized` → max_daily_loss nunca disparava."""
    db = tmp_path / "rs.db"
    await init_database(db)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    closed_iso = (now - timedelta(hours=2)).isoformat()  # dentro das 24h

    async with get_connection(db) as conn:
        # Position fechada com loss realizado de -50 (dentro das 24h)
        await conn.execute(
            "INSERT INTO bot_positions"
            " (id, condition_id, token_id, market_title, outcome,"
            "  size, avg_entry_price, current_price, realized_pnl,"
            "  source_wallets_json, opened_at, closed_at, is_open, updated_at)"
            " VALUES (1, '0xA', 'tok1', 'm', 'Yes',"
            "  100.0, 0.50, 0.0, -50.0, '[]', ?, ?, 0, ?)",
            (closed_iso, closed_iso, closed_iso),
        )
        # Position aberta com unrealized de +10
        await conn.execute(
            "INSERT INTO bot_positions"
            " (id, condition_id, token_id, market_title, outcome,"
            "  size, avg_entry_price, current_price, unrealized_pnl,"
            "  source_wallets_json, opened_at, is_open, updated_at)"
            " VALUES (2, '0xB', 'tok2', 'm', 'Yes',"
            "  100.0, 0.40, 0.50, 10.0, '[]', ?, 1, ?)",
            (now_iso, now_iso),
        )
        await conn.commit()

    async def fetch_balance() -> float:
        return 100.0

    bc = BalanceCache(fetch_balance)
    await bc.refresh_once()

    async with get_connection(db) as conn:
        rs = await build_risk_state(balance_cache=bc, conn=conn)

    # daily_pnl = realized_24h(-50) + unrealized(+10) = -40
    assert rs.daily_pnl == pytest.approx(-40.0)
    # total_realized_pnl é o lifetime; a position fechada não está mais
    # em is_open=1 portanto não conta na agregada principal — só a aberta.
    # Position 2 (open) tem realized_pnl=0 default → realized_lifetime=0.
    assert rs.total_realized_pnl == pytest.approx(0.0)
    assert rs.total_unrealized_pnl == pytest.approx(10.0)


# ============ Item 3 — Telegram parse_mode HTML + html.escape ============

@pytest.mark.asyncio
async def test_telegram_send_uses_html_parse_mode():
    """_send chama send_message com parse_mode='HTML'."""
    from src.notifier.telegram import TelegramNotifier
    n = TelegramNotifier(token="abc", chat_id="123")
    n._bot = MagicMock()
    n._bot.send_message = AsyncMock()
    await n._send("<b>test</b>")
    args, kwargs = n._bot.send_message.call_args
    assert kwargs["parse_mode"] == "HTML"
    assert kwargs["text"] == "<b>test</b>"


@pytest.mark.asyncio
async def test_telegram_escapes_market_title():
    """Títulos com `<`/`>` são escapados — não quebram parse HTML."""
    from src.core.models import CopyTrade, TradeSignal
    from src.notifier.telegram import TelegramNotifier

    n = TelegramNotifier(token="abc", chat_id="123")
    captured: dict = {}

    async def capture(text: str, *, silent: bool = False) -> None:
        captured["text"] = text
        captured["silent"] = silent

    n._send = capture  # type: ignore[method-assign]

    sig = TradeSignal.model_construct(
        id="s1", wallet_address="0xabc", wallet_score=0.8,
        condition_id="0xc", token_id="t", side="BUY",
        size=10.0, price=0.5, usd_value=5.0,
        market_title="Will <Microsoft> beat AAPL > 200B?",
        outcome="Yes",
        market_end_date=None, hours_to_resolution=24.0,
        detected_at=datetime.now(timezone.utc),
        source="websocket", status="pending", skip_reason=None,
    )
    trade = CopyTrade(
        id="t1", signal_id="s1", condition_id="0xc", token_id="t",
        side="BUY", intended_size=10.0, intended_price=0.5,
        status="pending", created_at=datetime.now(timezone.utc),
    )
    n.notify_trade(sig, trade)
    await asyncio.sleep(0.01)  # deixa a task fire-and-forget rodar
    text = captured["text"]
    # `<Microsoft>` virou `&lt;Microsoft&gt;` — sem isso quebra o parse
    assert "&lt;Microsoft&gt;" in text
    assert "<Microsoft>" not in text
    assert "&gt; 200B" in text
    # Tags de template (<b>) ficam intactas
    assert "<b>BUY</b>" in text


# ============ Item 4 — row_factory aiosqlite.Row ============

@pytest.mark.asyncio
async def test_shared_connection_uses_row_factory(tmp_path: Path):
    """open_shared_connection seta row_factory para acesso por nome."""
    db = tmp_path / "shared.db"
    await init_database(db)
    conn = await open_shared_connection(db)
    try:
        # Insere e lê — testa acesso por nome de coluna
        async with conn.execute(
            "INSERT INTO bot_positions"
            " (id, condition_id, token_id, market_title, outcome,"
            "  size, avg_entry_price, source_wallets_json,"
            "  opened_at, is_open, updated_at)"
            " VALUES (1, '0xC', 'tok', 'm', 'Yes', 100.0, 0.5, '[]',"
            "  datetime('now'), 1, datetime('now'))"
        ):
            pass
        await conn.commit()
        async with conn.execute(
            "SELECT id, condition_id, size FROM bot_positions WHERE id=1"
        ) as cur:
            row = await cur.fetchone()
        # ⚠️ Acesso por nome — só funciona com row_factory=aiosqlite.Row
        assert row["id"] == 1
        assert row["condition_id"] == "0xC"
        assert row["size"] == 100.0
        # Tuple unpacking ainda funciona (Row é compatível)
        rid, cid, size = row
        assert rid == 1 and cid == "0xC" and size == 100.0
    finally:
        await conn.close()


# ============ Item 5 — whale_add + tracker inline ============

def test_whale_add_increments_size():
    s = InMemoryState()
    n = s.whale_add("0xWHALE", "tok", 50.0)
    assert n == 50.0
    n = s.whale_add("0xWHALE", "tok", 30.0)
    assert n == 80.0


def test_whale_add_decrements_and_clamps_at_zero():
    """Over-sell (delta > size) clampa no zero. NÃO fica negativo."""
    s = InMemoryState()
    s.whale_set("0xW", "tok", 10.0)
    n = s.whale_add("0xW", "tok", -50.0)  # tenta vender 5x mais
    assert n == 0.0
    assert s.whale_size("0xW", "tok") == 0.0


def test_whale_add_lowercase_normalization():
    """address case-insensitive — bot_add e whale_add convergem na chave."""
    s = InMemoryState()
    s.whale_add("0xWHALE", "tok", 100.0)
    s.whale_add("0xwhale", "tok", 50.0)  # mesmo address case-different
    # Chave normalizada lowercase — soma na MESMA entry
    assert s.whale_size("0xWHALE", "tok") == 150.0
    assert s.whale_size("0xwhale", "tok") == 150.0


@pytest.mark.asyncio
async def test_trade_monitor_updates_whale_inventory_inline():
    """Após enqueue, state.whale_inventory reflete o trade IMEDIATAMENTE."""
    from src.tracker.trade_monitor import TradeMonitor

    state = InMemoryState()
    state.whale_set("0xwhale", "tok", 100.0)  # baseline

    queue: asyncio.Queue = asyncio.Queue(maxsize=10)
    cfg = MagicMock()
    cfg.signal_max_age_seconds = 3600
    cfg.min_trade_size_usd = 0
    cfg.market_duration_filter.enabled = False

    gamma = MagicMock()
    gamma.get_market = AsyncMock(return_value={"tokens": []})

    monitor = TradeMonitor(
        cfg=cfg, data_client=MagicMock(), gamma=gamma,
        ws_client=MagicMock(),
        wallet_scores={"0xwhale": 0.8}, queue=queue,
        state=state, conn=None,
    )

    # BUY de 30 unidades — sem currentSize → fallback delta
    await monitor._enqueue_trade({
        "maker": "0xWHALE", "conditionId": "0xC", "asset": "tok",
        "side": "BUY", "size": 30, "price": 0.5,
        "timestamp": int(datetime.now(timezone.utc).timestamp()),
    }, source="test")

    # Whale inventory atualizado IMEDIATAMENTE: 100 + 30 = 130
    assert state.whale_size("0xwhale", "tok") == 130.0


@pytest.mark.asyncio
async def test_trade_monitor_uses_current_whale_size_when_present():
    """current_whale_size disponível → whale_set (overrides drift)."""
    from src.tracker.trade_monitor import TradeMonitor

    state = InMemoryState()
    state.whale_set("0xwhale", "tok", 50.0)  # estado defasado

    queue: asyncio.Queue = asyncio.Queue(maxsize=10)
    cfg = MagicMock()
    cfg.signal_max_age_seconds = 3600
    cfg.min_trade_size_usd = 0
    cfg.market_duration_filter.enabled = False

    gamma = MagicMock()
    gamma.get_market = AsyncMock(return_value={"tokens": []})

    monitor = TradeMonitor(
        cfg=cfg, data_client=MagicMock(), gamma=gamma,
        ws_client=MagicMock(),
        wallet_scores={"0xwhale": 0.8}, queue=queue,
        state=state, conn=None,
    )

    # Trade BUY com currentSize=200 (saldo real pós-trade) — drift correction
    await monitor._enqueue_trade({
        "maker": "0xWHALE", "conditionId": "0xC", "asset": "tok",
        "side": "BUY", "size": 30, "price": 0.5,
        "currentSize": 200,
        "timestamp": int(datetime.now(timezone.utc).timestamp()),
    }, source="test")

    # Override com ground truth do payload (não 50 + 30 = 80)
    assert state.whale_size("0xwhale", "tok") == 200.0


# ============ Item 6 — metrics.halted gauge ============

def test_halt_sets_metric_to_one():
    """halt() seta gauge=1 → Grafana/Alertmanager detectam."""
    cfg = MagicMock()
    rm = RiskManager(cfg)
    rm.halt("test")
    # Lê o valor do gauge via samples interno
    samples = list(metrics.halted.collect())[0].samples
    val = samples[0].value
    assert val == 1.0


def test_resume_sets_metric_to_zero():
    """resume() volta gauge=0."""
    cfg = MagicMock()
    rm = RiskManager(cfg)
    rm.halt("test")
    rm.resume()
    samples = list(metrics.halted.collect())[0].samples
    val = samples[0].value
    assert val == 0.0
