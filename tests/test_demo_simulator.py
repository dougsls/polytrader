"""Cobertura do paper_simulator (24h DEMO REALISTA).

Garante que o bot NUNCA registra fill perfeito em paper:
  - sem book → SKIPPED BOOK_FETCH_FAIL
  - book vazio → SKIPPED DEPTH_EMPTY
  - spread > max → SKIPPED SPREAD_WIDE
  - slippage > tolerância → SKIPPED SLIPPAGE_HIGH
  - depth < 30% intended → SKIPPED DEPTH_TOO_THIN
  - tudo OK → executed @ VWAP real (não whale_price)
  - emergency exit (stale) → ignora spread/slippage mas exige book
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.database import get_connection, init_database
from src.core.models import TradeSignal
from src.dashboard.demo_persistence import record_journal_entry
from src.executor.paper_simulator import simulate_fill


def _cfg(slip: float = 0.03, spread: float = 0.02) -> object:
    cfg = MagicMock()
    cfg.whale_max_slippage_pct = slip
    cfg.max_spread = spread
    return cfg


def _signal(**overrides: object) -> TradeSignal:
    base = dict(
        id="s1", wallet_address="0xa", wallet_score=0.8,
        condition_id="0xC", token_id="tok", side="BUY",
        size=20.0, price=0.50, usd_value=10.0,
        market_title="X", outcome="Yes",
        market_end_date=None, hours_to_resolution=24.0,
        detected_at=datetime.now(timezone.utc),
        source="websocket", status="pending", skip_reason=None,
        bypass_slippage_check=False,
    )
    base.update(overrides)
    return TradeSignal.model_construct(**base)


# ============ Skip cases ============

def test_book_none_skips_book_fetch_fail():
    r = simulate_fill(
        signal=_signal(), book=None, intended_size=20.0,
        intended_price=0.51, cfg=_cfg(),
    )
    assert r.status == "skipped"
    assert r.skip_reason == "BOOK_FETCH_FAIL"
    assert r.simulated_size == 0.0
    assert not r.filled


def test_empty_book_skips_depth_empty():
    r = simulate_fill(
        signal=_signal(), book={"asks": [], "bids": []},
        intended_size=20.0, intended_price=0.51, cfg=_cfg(),
    )
    assert r.skip_reason == "DEPTH_EMPTY"


def test_wide_spread_skips_spread_wide():
    book = {
        "asks": [{"price": "0.55", "size": "100"}],
        "bids": [{"price": "0.45", "size": "100"}],
    }
    r = simulate_fill(
        signal=_signal(), book=book, intended_size=20.0,
        intended_price=0.55, cfg=_cfg(spread=0.02),
    )
    assert r.skip_reason == "SPREAD_WIDE"
    assert r.spread > 0.02


def test_high_slippage_skips_slippage_high():
    """whale comprou a 0.50; book agora está em 0.60. Slippage 20% > 3%."""
    book = {
        "asks": [{"price": "0.60", "size": "100"}],
        "bids": [{"price": "0.59", "size": "100"}],
    }
    r = simulate_fill(
        signal=_signal(price=0.50), book=book,
        intended_size=20.0, intended_price=0.60, cfg=_cfg(slip=0.03),
    )
    assert r.skip_reason == "SLIPPAGE_HIGH"
    assert r.slippage > 0.03


def test_thin_depth_skips_depth_too_thin():
    """Book L1 cobre só 10% do intended → SKIPPED."""
    # intended 20 tokens × 0.51 = 10.2 USD; book cobre 1 USD = 10%
    book = {
        "asks": [{"price": "0.51", "size": "2"}],   # capacity 1.02 USD
        "bids": [{"price": "0.50", "size": "100"}],
    }
    r = simulate_fill(
        signal=_signal(), book=book,
        intended_size=20.0, intended_price=0.51, cfg=_cfg(),
    )
    assert r.skip_reason == "DEPTH_TOO_THIN"


# ============ Fill cases ============

def test_executed_uses_vwap_not_whale_price():
    """Walk de 2 níveis → simulated_price = VWAP, não whale_price."""
    book = {
        "asks": [
            {"price": "0.50", "size": "10"},   # 5 USD
            {"price": "0.51", "size": "20"},   # 10.2 USD
        ],
        "bids": [{"price": "0.49", "size": "100"}],
    }
    r = simulate_fill(
        signal=_signal(price=0.50), book=book,
        intended_size=20.0, intended_price=0.51, cfg=_cfg(),
    )
    assert r.status == "executed"
    # VWAP = (10×0.50 + 10×0.51)/20 = 0.505
    assert r.simulated_price == pytest.approx(0.505, rel=1e-3)
    assert r.simulated_price != r.whale_price


def test_partial_fill_when_depth_below_intended_but_above_min():
    """Depth cobre 50% do intended (>30% min) → PARTIAL."""
    # intended 20 × 0.51 = 10.2 USD. Book = 5 USD = ~49% do intended.
    book = {
        "asks": [{"price": "0.50", "size": "10"}],   # 5 USD = 49%
        "bids": [{"price": "0.49", "size": "100"}],
    }
    r = simulate_fill(
        signal=_signal(), book=book,
        intended_size=20.0, intended_price=0.51, cfg=_cfg(),
    )
    assert r.status == "partial"
    assert r.simulated_size < 20.0
    assert r.simulated_size >= 0.30 * 20.0  # acima do min_fill_pct


# ============ Emergency exit ============

def test_emergency_exit_ignores_spread_and_slippage():
    """bypass_slippage_check=True (stale cleanup) ignora cap de spread+slip
    mas ainda exige book não vazio."""
    book = {
        "asks": [{"price": "0.99", "size": "100"}],
        "bids": [{"price": "0.05", "size": "100"}],   # spread 94c — absurdo
    }
    sig = _signal(side="SELL", price=0.50, bypass_slippage_check=True)
    r = simulate_fill(
        signal=sig, book=book, intended_size=20.0,
        intended_price=0.05, cfg=_cfg(spread=0.02),
    )
    assert r.is_emergency_exit
    assert r.status in ("executed", "partial")


def test_emergency_exit_still_needs_book():
    """Mesmo emergency exit não cria fill em book vazio."""
    sig = _signal(side="SELL", bypass_slippage_check=True)
    r = simulate_fill(
        signal=sig, book={"asks": [], "bids": []},
        intended_size=20.0, intended_price=0.50, cfg=_cfg(),
    )
    assert r.skip_reason == "DEPTH_EMPTY"
    assert not r.filled


# ============ Persistência journal ============

@pytest.mark.asyncio
async def test_record_journal_entry_persists_skip(tmp_path: Path):
    db = tmp_path / "j.db"
    await init_database(db)
    # Seed signal pra FK
    now_iso = datetime.now(timezone.utc).isoformat()
    async with get_connection(db) as conn:
        await conn.execute(
            "INSERT INTO tracked_wallets (address, score, tracked_since, updated_at)"
            " VALUES (?,?,?,?)", ("0xa", 0.8, now_iso, now_iso),
        )
        await conn.execute(
            "INSERT INTO trade_signals"
            " (id, wallet_address, wallet_score, condition_id, token_id, side,"
            "  size, price, usd_value, market_title, outcome, detected_at, source)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("s-j", "0xa", 0.8, "0xC", "tok", "BUY", 20.0, 0.50, 10.0,
             "X", "Yes", now_iso, "websocket"),
        )
        await conn.commit()

    sig = _signal(id="s-j")
    sim = simulate_fill(
        signal=sig, book={"asks": [], "bids": []},
        intended_size=20.0, intended_price=0.51, cfg=_cfg(),
    )
    await record_journal_entry(
        signal=sig, sim=sim, intended_size_usd=10.2,
        source="websocket", db_path=db,
    )
    async with get_connection(db) as conn:
        async with conn.execute(
            "SELECT signal_id, status, skip_reason, intended_size, simulated_size"
            " FROM demo_journal WHERE signal_id=?", ("s-j",),
        ) as cur:
            row = await cur.fetchone()
    assert row is not None
    assert row["signal_id"] == "s-j"
    assert row["status"] == "skipped"
    assert row["skip_reason"] == "DEPTH_EMPTY"
    assert row["intended_size"] == 20.0
    assert row["simulated_size"] == 0.0


@pytest.mark.asyncio
async def test_record_journal_entry_persists_executed(tmp_path: Path):
    db = tmp_path / "j2.db"
    await init_database(db)
    now_iso = datetime.now(timezone.utc).isoformat()
    async with get_connection(db) as conn:
        await conn.execute(
            "INSERT INTO tracked_wallets (address, score, tracked_since, updated_at)"
            " VALUES (?,?,?,?)", ("0xa", 0.8, now_iso, now_iso),
        )
        await conn.execute(
            "INSERT INTO trade_signals"
            " (id, wallet_address, wallet_score, condition_id, token_id, side,"
            "  size, price, usd_value, market_title, outcome, detected_at, source)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("s-e", "0xa", 0.8, "0xC", "tok", "BUY", 20.0, 0.50, 10.0,
             "X", "Yes", now_iso, "websocket"),
        )
        await conn.commit()

    sig = _signal(id="s-e", price=0.50)
    book = {
        "asks": [{"price": "0.50", "size": "100"}],
        "bids": [{"price": "0.49", "size": "100"}],
    }
    sim = simulate_fill(
        signal=sig, book=book, intended_size=20.0,
        intended_price=0.51, cfg=_cfg(),
    )
    await record_journal_entry(
        signal=sig, sim=sim, intended_size_usd=10.2,
        source="websocket", db_path=db,
    )
    async with get_connection(db) as conn:
        async with conn.execute(
            "SELECT status, simulated_price, slippage FROM demo_journal"
            " WHERE signal_id='s-e'"
        ) as cur:
            row = await cur.fetchone()
    assert row["status"] == "executed"
    assert row["simulated_price"] > 0


# ============ Config demo realista ============

def test_demo_yaml_loads_with_correct_defaults(tmp_path: Path):
    """config.demo.yaml carrega com banca $200, paper_perfect_mirror=false,
    todos os filtros live ligados."""
    from pathlib import Path as P
    from src.core.config import load_yaml_config

    demo_path = P("config.demo.yaml")
    if not demo_path.exists():
        pytest.skip("config.demo.yaml not present")
    cfg = load_yaml_config(demo_path)
    ex = cfg.executor
    assert ex.mode == "paper"
    assert ex.paper_perfect_mirror is False
    assert ex.max_portfolio_usd == 200.0
    assert ex.copy_buys is True
    assert ex.copy_sells is True
    assert ex.avoid_resolved_markets is True
    assert ex.enforce_market_duration is True
    assert ex.whale_max_slippage_pct <= 0.05
    assert ex.max_spread <= 0.05
    assert cfg.depth_sizing.enabled is True
