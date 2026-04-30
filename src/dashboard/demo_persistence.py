"""24h DEMO REALISTA — persistência do journal + snapshots de banca.

API mínima e tipada usada pelo `copy_engine` (`record_journal_entry`)
e pelo loop de snapshots (`record_bank_snapshot`, `record_readiness`).

Mantida fora do `copy_engine` por separação de concerns: hot path
trading não deveria importar agregadores de dashboard.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from src.core.database import DEFAULT_DB_PATH, get_connection
from src.core.logger import get_logger
from src.core.models import TradeSignal
from src.executor.paper_simulator import SimulationResult

log = get_logger(__name__)


async def record_journal_entry(
    *,
    signal: TradeSignal,
    sim: SimulationResult,
    intended_size_usd: float,
    source: str,
    latency_signal_to_decision_ms: float | None = None,
    latency_signal_to_fill_ms: float | None = None,
    extras: dict[str, Any] | None = None,
    conn: aiosqlite.Connection | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    """Persiste uma entrada do `demo_journal` para AUDITABILIDADE.

    Chamado pelo `copy_engine` em CADA decisão (executed/partial/skipped).
    Falha silente em caso de erro — auditoria não pode quebrar trade.
    """
    now = datetime.now(timezone.utc).isoformat()
    extras_json = json.dumps(extras) if extras else None
    sql = (
        "INSERT INTO demo_journal ("
        " timestamp, signal_id, wallet_address, wallet_score,"
        " confluence_count, condition_id, token_id, market_title, outcome,"
        " side, whale_price, whale_size, whale_usd, ref_price,"
        " simulated_price, spread, slippage, intended_size, intended_size_usd,"
        " simulated_size, simulated_size_usd, depth_usd, levels_consumed,"
        " status, skip_reason, is_emergency_exit, source,"
        " latency_signal_to_decision_ms, latency_signal_to_fill_ms,"
        " extras_json"
        ") VALUES ("
        " ?, ?, ?, ?,"
        " ?, ?, ?, ?, ?,"
        " ?, ?, ?, ?, ?,"
        " ?, ?, ?, ?, ?,"
        " ?, ?, ?, ?,"
        " ?, ?, ?, ?,"
        " ?, ?,"
        " ?"
        ")"
    )
    params = (
        now, signal.id, signal.wallet_address, signal.wallet_score,
        signal.confluence_count, signal.condition_id, signal.token_id,
        signal.market_title, signal.outcome,
        signal.side, sim.whale_price, signal.size, signal.usd_value,
        sim.ref_price, sim.simulated_price, sim.spread, sim.slippage,
        sim.intended_size, intended_size_usd,
        sim.simulated_size,
        sim.simulated_size * sim.simulated_price if sim.simulated_price > 0 else 0.0,
        sim.depth_usd, sim.levels_consumed,
        sim.status, sim.skip_reason, 1 if sim.is_emergency_exit else 0,
        source, latency_signal_to_decision_ms, latency_signal_to_fill_ms,
        extras_json,
    )
    try:
        if conn is not None:
            await conn.execute(sql, params)
            await conn.commit()
        else:
            async with get_connection(db_path) as db:
                await db.execute(sql, params)
                await db.commit()
    except Exception as exc:  # noqa: BLE001 — auditoria não bloqueia
        log.error("demo_journal_persist_failed",
                  signal_id=signal.id, err=repr(exc)[:120])


async def record_bank_snapshot(
    *,
    starting_bank_usd: float,
    cash_available: float,
    invested_usd: float,
    market_value_usd: float,
    realized_pnl: float,
    unrealized_pnl: float,
    peak_bank: float,
    open_positions: int,
    counters: dict[str, int],
    health: dict[str, Any],
    conn: aiosqlite.Connection | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    """Persiste snapshot da banca demo (a cada N seg)."""
    now = datetime.now(timezone.utc).isoformat()
    bank_total = cash_available + market_value_usd
    total_pnl = realized_pnl + unrealized_pnl
    roi_pct = (
        ((bank_total - starting_bank_usd) / starting_bank_usd) * 100.0
        if starting_bank_usd > 0 else 0.0
    )
    current_dd = (
        max(peak_bank - bank_total, 0.0) / peak_bank * 100.0
        if peak_bank > 0 else 0.0
    )
    sql = (
        "INSERT INTO demo_bank_snapshots ("
        " timestamp, starting_bank_usd, cash_available, invested_usd,"
        " market_value_usd, bank_total, realized_pnl, unrealized_pnl,"
        " total_pnl, roi_pct, peak_bank, current_drawdown_pct, max_drawdown_pct,"
        " open_positions, signals_total, signals_executed, signals_skipped,"
        " signals_partial, signals_dropped, win_count, loss_count,"
        " rtds_connected, polling_active, geoblock_status, queue_size"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    params = (
        now, starting_bank_usd, cash_available, invested_usd,
        market_value_usd, bank_total, realized_pnl, unrealized_pnl,
        total_pnl, roi_pct, peak_bank, current_dd, current_dd,  # max_dd same; aggregator computa real
        open_positions,
        counters.get("total", 0), counters.get("executed", 0),
        counters.get("skipped", 0), counters.get("partial", 0),
        counters.get("dropped", 0), counters.get("wins", 0),
        counters.get("losses", 0),
        1 if health.get("rtds_connected") else 0,
        1 if health.get("polling_active") else 0,
        str(health.get("geoblock_status") or ""),
        int(health.get("queue_size") or 0),
    )
    try:
        if conn is not None:
            await conn.execute(sql, params)
            await conn.commit()
        else:
            async with get_connection(db_path) as db:
                await db.execute(sql, params)
                await db.commit()
    except Exception as exc:  # noqa: BLE001
        log.error("bank_snapshot_persist_failed", err=repr(exc)[:120])


async def record_readiness(
    *,
    score: int,
    recommendation: str,
    components: dict[str, int],
    no_go_flags: dict[str, bool],
    details: dict[str, Any] | None = None,
    conn: aiosqlite.Connection | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    details_json = json.dumps(details) if details else None
    sql = (
        "INSERT INTO demo_readiness_snapshots ("
        " timestamp, score, recommendation,"
        " roi_component, drawdown_component, slippage_component,"
        " audit_component, api_health_component, skip_rate_component,"
        " sample_size_component, pnl_concentration_component,"
        " drawdown_no_go, state_divergence_no_go, fake_fills_no_go,"
        " api_errors_no_go, slippage_no_go, pnl_outlier_no_go,"
        " details_json"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    params = (
        now, score, recommendation,
        components.get("roi", 0), components.get("drawdown", 0),
        components.get("slippage", 0), components.get("audit", 0),
        components.get("api_health", 0), components.get("skip_rate", 0),
        components.get("sample_size", 0), components.get("pnl_concentration", 0),
        1 if no_go_flags.get("drawdown") else 0,
        1 if no_go_flags.get("state_divergence") else 0,
        1 if no_go_flags.get("fake_fills") else 0,
        1 if no_go_flags.get("api_errors") else 0,
        1 if no_go_flags.get("slippage") else 0,
        1 if no_go_flags.get("pnl_outlier") else 0,
        details_json,
    )
    try:
        if conn is not None:
            await conn.execute(sql, params)
            await conn.commit()
        else:
            async with get_connection(db_path) as db:
                await db.execute(sql, params)
                await db.commit()
    except Exception as exc:  # noqa: BLE001
        log.error("readiness_persist_failed", err=repr(exc)[:120])
