"""Endpoints da demo + readiness score lógica."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.database import get_connection, init_database
from src.dashboard.demo_aggregators import (
    equity_curve,
    execution_quality,
    readiness_overview,
    risk_exposure,
    trade_journal,
    trader_attribution,
)
from src.dashboard.readiness_score import compute_readiness


async def _seed(db: Path) -> None:
    await init_database(db)
    now_iso = datetime.now(timezone.utc).isoformat()
    async with get_connection(db) as conn:
        await conn.execute(
            "INSERT INTO tracked_wallets (address, score, win_rate, pnl_usd,"
            " tracked_since, updated_at, name)"
            " VALUES (?,?,?,?,?,?,?)",
            ("0xwhale", 0.85, 0.65, 50000.0, now_iso, now_iso, "TestWhale"),
        )
        await conn.execute(
            "INSERT INTO trade_signals"
            " (id, wallet_address, wallet_score, condition_id, token_id, side,"
            "  size, price, usd_value, market_title, outcome, detected_at,"
            "  source, status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("sig-1", "0xwhale", 0.85, "0xC1", "tok1", "BUY",
             10.0, 0.5, 5.0, "M1", "Yes", now_iso, "websocket", "executed"),
        )
        # Journal: 1 executed + 1 skipped
        for status, slip, reason in [("executed", 0.015, None),
                                      ("skipped", 0.0, "DEPTH_TOO_THIN")]:
            await conn.execute(
                "INSERT INTO demo_journal"
                " (timestamp, signal_id, wallet_address, wallet_score,"
                "  confluence_count, condition_id, token_id, market_title,"
                "  outcome, side, whale_price, whale_size, whale_usd, ref_price,"
                "  simulated_price, spread, slippage, intended_size,"
                "  intended_size_usd, simulated_size, simulated_size_usd,"
                "  depth_usd, levels_consumed, status, skip_reason,"
                "  is_emergency_exit, source)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now_iso, "sig-1", "0xwhale", 0.85, 1, "0xC1", "tok1", "M1",
                 "Yes", "BUY", 0.50, 10.0, 5.0, 0.51, 0.51, 0.01, slip,
                 10.0, 5.1, 10.0 if status == "executed" else 0.0,
                 5.1 if status == "executed" else 0.0, 5.1, 1, status, reason,
                 0, "websocket"),
            )
        # Position aberta
        await conn.execute(
            "INSERT INTO bot_positions"
            " (condition_id, token_id, market_title, outcome, size,"
            "  avg_entry_price, current_price, unrealized_pnl,"
            "  source_wallets_json, opened_at, is_open, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("0xC1", "tok1", "M1", "Yes", 10.0, 0.51, 0.55, 0.40,
             '["0xwhale"]', now_iso, 1, now_iso),
        )
        await conn.commit()


@pytest.mark.asyncio
async def test_readiness_overview(tmp_path: Path):
    db = tmp_path / "ro.db"
    await _seed(db)
    async with get_connection(db) as conn:
        data = await readiness_overview(conn, starting_bank_usd=200.0)
    assert data["starting_bank_usd"] == 200.0
    assert data["open_positions"] == 1
    assert data["unrealized_pnl"] == pytest.approx(0.40)
    assert data["bank_total"] == pytest.approx(200.40)
    assert "signals_24h" in data
    assert "journal_24h" in data


@pytest.mark.asyncio
async def test_execution_quality_breakdown(tmp_path: Path):
    db = tmp_path / "eq.db"
    await _seed(db)
    async with get_connection(db) as conn:
        eq = await execution_quality(conn)
    assert "BUY" in eq
    assert eq["BUY"]["count"] >= 1
    assert eq["BUY"]["avg_slippage"] > 0
    assert any(s["reason"] == "DEPTH_TOO_THIN" for s in eq["skip_reasons"])


@pytest.mark.asyncio
async def test_trader_attribution(tmp_path: Path):
    db = tmp_path / "ta.db"
    await _seed(db)
    async with get_connection(db) as conn:
        rows = await trader_attribution(conn)
    assert len(rows) == 1
    r = rows[0]
    assert r["wallet"] == "0xwhale"
    assert r["signals_detected"] == 2
    assert r["signals_executed"] == 1
    assert r["signals_skipped"] == 1


@pytest.mark.asyncio
async def test_risk_exposure(tmp_path: Path):
    db = tmp_path / "re.db"
    await _seed(db)
    async with get_connection(db) as conn:
        rx = await risk_exposure(conn)
    assert len(rx["by_market"]) == 1
    assert rx["by_market"][0]["market_value_usd"] == pytest.approx(5.5)


@pytest.mark.asyncio
async def test_trade_journal_with_filters(tmp_path: Path):
    db = tmp_path / "tj.db"
    await _seed(db)
    async with get_connection(db) as conn:
        all_rows = await trade_journal(conn, limit=100)
        skipped = await trade_journal(conn, limit=100, status="skipped")
    assert len(all_rows) == 2
    assert len(skipped) == 1
    assert skipped[0]["skip_reason"] == "DEPTH_TOO_THIN"


@pytest.mark.asyncio
async def test_equity_curve_empty_returns_list(tmp_path: Path):
    db = tmp_path / "ec.db"
    await _seed(db)
    async with get_connection(db) as conn:
        pts = await equity_curve(conn, hours=24)
    assert isinstance(pts, list)


# ============ Readiness Score ============

@pytest.mark.asyncio
async def test_readiness_score_no_data_returns_extend(tmp_path: Path):
    """Sem dados: score acima de 50 (componentes saudáveis em zero)
    mas amostra=0 e ROI=0 trazem pra EXTEND_PAPER (não GO sem dados)."""
    db = tmp_path / "rs1.db"
    await init_database(db)
    async with get_connection(db) as conn:
        result = await compute_readiness(conn, starting_bank_usd=200.0)
    # Sem ROI positivo NÃO entra em GO (componente roi=0)
    assert result.components["roi"] == 0
    assert result.components["sample_size"] == 0
    assert result.recommendation in ("EXTEND_PAPER", "NO-GO")


@pytest.mark.asyncio
async def test_readiness_score_drawdown_no_go(tmp_path: Path):
    """Drawdown > 10% = NO-GO automático mesmo com bom ROI."""
    db = tmp_path / "rs2.db"
    await init_database(db)
    now_iso = datetime.now(timezone.utc).isoformat()
    async with get_connection(db) as conn:
        await conn.execute(
            "INSERT INTO demo_bank_snapshots"
            " (timestamp, starting_bank_usd, cash_available, invested_usd,"
            "  market_value_usd, bank_total, realized_pnl, unrealized_pnl,"
            "  total_pnl, roi_pct, peak_bank, current_drawdown_pct,"
            "  max_drawdown_pct, open_positions, signals_total,"
            "  signals_executed, signals_skipped, signals_partial,"
            "  signals_dropped, win_count, loss_count,"
            "  rtds_connected, polling_active, geoblock_status, queue_size)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso, 200.0, 100.0, 50.0, 30.0, 130.0, -70.0, 0.0,
             -70.0, -35.0, 200.0, 35.0, 35.0, 1, 10, 5, 5, 0, 0, 2, 3,
             1, 1, "ok", 0),
        )
        await conn.commit()
        result = await compute_readiness(conn, starting_bank_usd=200.0)
    assert result.recommendation == "NO-GO"
    assert result.no_go_flags.get("drawdown") is True


@pytest.mark.asyncio
async def test_readiness_score_calculates_components(tmp_path: Path):
    """Componentes devem somar pro score total."""
    db = tmp_path / "rs3.db"
    await _seed(db)
    async with get_connection(db) as conn:
        result = await compute_readiness(conn, starting_bank_usd=200.0)
    total = sum(result.components.values())
    assert result.score == total
