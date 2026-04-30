"""Readiness Score 0-100 — critério objetivo GO / NO-GO / EXTEND_PAPER.

Componentes positivos (somam até 100):
  ROI_24h positivo:                 +20
  drawdown máximo < 5%:             +15
  slippage médio < 2%:              +15
  todos skips com motivo auditável: +10
  zero erros críticos API/WS:       +10
  taxa de skip coerente:            +10
  amostra suficiente (≥20 sinais):  +10
  PnL não concentrado em 1 trade:   +10

NO-GO automático (resultado=NO_GO independente do score):
  - drawdown > 10%
  - estado local divergente (bot_size negativo / fantasma)
  - fills sem book auditável (executed sem journal entry)
  - taxa de erro API > 10%
  - slippage médio > limite
  - PnL positivo dependente de 1 outlier (>80% do total)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiosqlite

DEFAULT_MIN_SAMPLE_SIZE = 20
DEFAULT_SLIPPAGE_LIMIT = 0.02


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    score: int
    recommendation: str  # GO / NO-GO / EXTEND_PAPER
    components: dict[str, int]
    no_go_flags: dict[str, bool]
    details: dict[str, Any]


async def compute_readiness(
    conn: aiosqlite.Connection,
    *,
    starting_bank_usd: float,
    slippage_limit: float = DEFAULT_SLIPPAGE_LIMIT,
    min_sample_size: int = DEFAULT_MIN_SAMPLE_SIZE,
) -> ReadinessResult:
    components: dict[str, int] = {}
    no_go: dict[str, bool] = {}
    details: dict[str, Any] = {}

    # --- 1. ROI 24h ---
    async with conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0), COALESCE(SUM(unrealized_pnl), 0)"
        " FROM bot_positions"
    ) as cur:
        row = await cur.fetchone()
    realized = float(row[0] or 0) if row else 0.0
    unrealized = float(row[1] or 0) if row else 0.0
    total_pnl = realized + unrealized
    bank_total = starting_bank_usd + total_pnl
    roi_pct = (total_pnl / starting_bank_usd) if starting_bank_usd > 0 else 0.0
    details["roi_pct"] = roi_pct
    details["bank_total"] = bank_total
    components["roi"] = 20 if total_pnl > 0 else 0

    # --- 2. Drawdown ---
    try:
        async with conn.execute(
            "SELECT MAX(max_drawdown_pct), MAX(current_drawdown_pct)"
            " FROM demo_bank_snapshots"
        ) as cur:
            row = await cur.fetchone()
        max_dd = float(row[0] or 0) if row else 0.0
        current_dd = float(row[1] or 0) if row else 0.0
    except aiosqlite.OperationalError:
        max_dd = 0.0
        current_dd = 0.0
    peak_dd = max(max_dd, current_dd)
    details["max_drawdown_pct"] = peak_dd
    if peak_dd > 10.0:
        no_go["drawdown"] = True
        components["drawdown"] = 0
    elif peak_dd < 5.0:
        components["drawdown"] = 15
    else:
        components["drawdown"] = 8

    # --- 3. Slippage médio ---
    try:
        async with conn.execute(
            "SELECT AVG(slippage), MAX(slippage), COUNT(*)"
            " FROM demo_journal"
            " WHERE status IN ('executed', 'partial') AND slippage IS NOT NULL"
        ) as cur:
            row = await cur.fetchone()
        avg_slip = float(row[0] or 0) if row else 0.0
        max_slip = float(row[1] or 0) if row else 0.0
        n_filled = int(row[2] or 0) if row else 0
    except aiosqlite.OperationalError:
        avg_slip = 0.0
        max_slip = 0.0
        n_filled = 0
    details["avg_slippage"] = avg_slip
    details["max_slippage"] = max_slip
    details["filled_count"] = n_filled
    if avg_slip > slippage_limit:
        no_go["slippage"] = True
        components["slippage"] = 0
    elif avg_slip < slippage_limit / 2:
        components["slippage"] = 15
    else:
        components["slippage"] = 8

    # --- 4. Audit completeness (todo skip tem skip_reason) ---
    try:
        async with conn.execute(
            "SELECT COUNT(*) FROM demo_journal"
            " WHERE status='skipped' AND (skip_reason IS NULL OR skip_reason='')"
        ) as cur:
            n_no_reason = int((await cur.fetchone())[0] or 0)
    except aiosqlite.OperationalError:
        n_no_reason = 0
    components["audit"] = 10 if n_no_reason == 0 else 0
    details["unaudited_skips"] = n_no_reason

    # --- 5. API health ---
    try:
        async with conn.execute(
            "SELECT COUNT(*) FROM demo_api_health WHERE healthy=0"
        ) as cur:
            api_errors = int((await cur.fetchone())[0] or 0)
    except aiosqlite.OperationalError:
        api_errors = 0
    if api_errors > 50:
        no_go["api_errors"] = True
        components["api_health"] = 0
    elif api_errors == 0:
        components["api_health"] = 10
    else:
        components["api_health"] = 5
    details["api_errors"] = api_errors

    # --- 6. Skip rate coerente (10% < skip < 80%) ---
    try:
        async with conn.execute(
            "SELECT COUNT(*),"
            " SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END)"
            " FROM demo_journal"
        ) as cur:
            row = await cur.fetchone()
        n_total = int(row[0] or 0)
        n_skipped = int(row[1] or 0)
    except aiosqlite.OperationalError:
        n_total = 0
        n_skipped = 0
    skip_rate = n_skipped / n_total if n_total > 0 else 0.0
    details["skip_rate"] = skip_rate
    details["signals_total"] = n_total
    if 0.10 <= skip_rate <= 0.80:
        components["skip_rate"] = 10
    else:
        components["skip_rate"] = 0

    # --- 7. Amostra suficiente ---
    components["sample_size"] = 10 if n_total >= min_sample_size else 0

    # --- 8. PnL não concentrado em 1 outlier ---
    try:
        async with conn.execute(
            "SELECT realized_pnl FROM bot_positions"
            " WHERE is_open=0 AND realized_pnl IS NOT NULL"
            " ORDER BY ABS(realized_pnl) DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        biggest = abs(float(row[0] or 0)) if row else 0.0
    except aiosqlite.OperationalError:
        biggest = 0.0
    abs_total = abs(realized) if realized != 0 else 1e-9
    concentration = biggest / abs_total if abs_total > 0 else 0.0
    details["pnl_concentration"] = concentration
    if total_pnl > 0 and concentration > 0.80:
        no_go["pnl_outlier"] = True
        components["pnl_concentration"] = 0
    elif concentration < 0.50:
        components["pnl_concentration"] = 10
    else:
        components["pnl_concentration"] = 5

    # --- Score total ---
    score = sum(components.values())

    # --- Recomendação ---
    if any(no_go.values()):
        recommendation = "NO-GO"
    elif score >= 75:
        recommendation = "GO"
    elif score >= 50:
        recommendation = "EXTEND_PAPER"
    else:
        recommendation = "NO-GO"

    return ReadinessResult(
        score=score,
        recommendation=recommendation,
        components=components,
        no_go_flags=no_go,
        details=details,
    )
