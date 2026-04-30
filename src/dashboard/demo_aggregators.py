"""Agregadores SQL para o dashboard demo. Funções puras async que
recebem `aiosqlite.Connection` e retornam dicts JSON-ready.

Mantido fora do app.py pra facilitar testes e manter `app.py` enxuto.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_hours_ago(h: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()


# ============ Readiness cards (24h Demo Readiness) ============

async def readiness_overview(
    conn: aiosqlite.Connection, *, starting_bank_usd: float,
) -> dict[str, Any]:
    """Cards principais da tela 24h Demo Readiness."""
    # Posições e PnL
    async with conn.execute(
        "SELECT COUNT(*),"
        " COALESCE(SUM(size * avg_entry_price), 0),"
        " COALESCE(SUM(size * COALESCE(current_price, avg_entry_price)), 0),"
        " COALESCE(SUM(unrealized_pnl), 0)"
        " FROM bot_positions WHERE is_open=1"
    ) as cur:
        row = await cur.fetchone()
    open_positions = int(row[0]) if row else 0
    invested = float(row[1]) if row else 0.0
    market_value = float(row[2]) if row else 0.0
    unrealized = float(row[3]) if row else 0.0

    async with conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0),"
        " SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN realized_pnl<0 THEN 1 ELSE 0 END)"
        " FROM bot_positions WHERE is_open=0"
    ) as cur:
        row = await cur.fetchone()
    realized = float(row[0]) if row else 0.0
    wins = int(row[1] or 0) if row else 0
    losses = int(row[2] or 0) if row else 0

    total_pnl = realized + unrealized
    bank_total = starting_bank_usd + total_pnl
    cash_available = max(bank_total - invested, 0.0)
    roi_pct = (total_pnl / starting_bank_usd) * 100.0 if starting_bank_usd > 0 else 0.0

    # Drawdown — peak vs current via demo_bank_snapshots
    peak_bank = starting_bank_usd
    max_dd = 0.0
    try:
        async with conn.execute(
            "SELECT MAX(bank_total), MAX(max_drawdown_pct) FROM demo_bank_snapshots"
        ) as cur:
            row = await cur.fetchone()
        if row and row[0]:
            peak_bank = max(float(row[0]), starting_bank_usd)
        if row and row[1]:
            max_dd = float(row[1])
    except aiosqlite.OperationalError:
        pass  # tabela ainda não existe (migration não aplicada)

    current_dd = (
        ((peak_bank - bank_total) / peak_bank) * 100.0 if peak_bank > 0 else 0.0
    )
    win_rate = wins / (wins + losses) if (wins + losses) > 0 else 0.0

    # Contadores 24h
    counters = await _signal_counts_24h(conn)
    journal = await _journal_counts_24h(conn)

    # Latências (p50/p95) últimas N probes
    latencies = await _latency_summary(conn)

    return {
        "starting_bank_usd": starting_bank_usd,
        "bank_total": bank_total,
        "cash_available": cash_available,
        "invested_usd": invested,
        "market_value_usd": market_value,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "total_pnl": total_pnl,
        "roi_pct": roi_pct,
        "peak_bank": peak_bank,
        "current_drawdown_pct": current_dd,
        "max_drawdown_pct": max(max_dd, current_dd),
        "win_rate": win_rate,
        "wins": wins,
        "losses": losses,
        "open_positions": open_positions,
        "signals_24h": counters,
        "journal_24h": journal,
        "latencies": latencies,
    }


async def _signal_counts_24h(conn: aiosqlite.Connection) -> dict[str, int]:
    cutoff = _iso_hours_ago(24)
    async with conn.execute(
        "SELECT status, COUNT(*) FROM trade_signals"
        " WHERE detected_at >= ? GROUP BY status",
        (cutoff,),
    ) as cur:
        rows = await cur.fetchall()
    out = {row[0]: int(row[1]) for row in rows}
    out["total"] = sum(out.values())
    return out


async def _journal_counts_24h(conn: aiosqlite.Connection) -> dict[str, int]:
    cutoff = _iso_hours_ago(24)
    try:
        async with conn.execute(
            "SELECT status, COUNT(*) FROM demo_journal"
            " WHERE timestamp >= ? GROUP BY status",
            (cutoff,),
        ) as cur:
            rows = await cur.fetchall()
        return {row[0]: int(row[1]) for row in rows}
    except aiosqlite.OperationalError:
        return {}


async def _latency_summary(conn: aiosqlite.Connection) -> dict[str, dict[str, float]]:
    """p50/p95 RTT por target (clob/gamma/data_api/rtds_ws)."""
    cutoff = _iso_hours_ago(1)
    out: dict[str, dict[str, float]] = {}
    try:
        async with conn.execute(
            "SELECT target, rtt_ms FROM latency_probes"
            " WHERE timestamp >= ? ORDER BY target, rtt_ms",
            (cutoff,),
        ) as cur:
            rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return out
    by_target: dict[str, list[float]] = {}
    for r in rows:
        by_target.setdefault(r[0], []).append(float(r[1]))
    for tgt, vals in by_target.items():
        n = len(vals)
        if n == 0:
            continue
        p50 = vals[n // 2]
        p95 = vals[min(int(n * 0.95), n - 1)]
        out[tgt] = {"p50_ms": p50, "p95_ms": p95, "n": n}
    return out


# ============ Execution Quality ============

async def execution_quality(conn: aiosqlite.Connection) -> dict[str, Any]:
    """Slippage/spread médios + breakdown de skip reasons."""
    cutoff = _iso_hours_ago(24)
    out: dict[str, Any] = {}
    try:
        # Médias por side
        async with conn.execute(
            "SELECT side, COUNT(*), AVG(slippage), MAX(slippage),"
            " AVG(spread), AVG(depth_usd)"
            " FROM demo_journal WHERE timestamp >= ? AND status IN ('executed', 'partial')"
            " GROUP BY side",
            (cutoff,),
        ) as cur:
            for r in await cur.fetchall():
                out[r[0]] = {
                    "count": int(r[1]),
                    "avg_slippage": float(r[2] or 0),
                    "max_slippage": float(r[3] or 0),
                    "avg_spread": float(r[4] or 0),
                    "avg_depth_usd": float(r[5] or 0),
                }
        # Breakdown skip reasons
        async with conn.execute(
            "SELECT skip_reason, COUNT(*) FROM demo_journal"
            " WHERE timestamp >= ? AND status='skipped' AND skip_reason IS NOT NULL"
            " GROUP BY skip_reason ORDER BY COUNT(*) DESC",
            (cutoff,),
        ) as cur:
            out["skip_reasons"] = [
                {"reason": r[0], "count": int(r[1])} for r in await cur.fetchall()
            ]
        # Comparações whale vs bot (top 50 trades executados)
        async with conn.execute(
            "SELECT signal_id, side, whale_price, simulated_price,"
            " (simulated_price - whale_price) AS diff,"
            " slippage, status"
            " FROM demo_journal WHERE timestamp >= ?"
            " AND status IN ('executed', 'partial')"
            " ORDER BY timestamp DESC LIMIT 50",
            (cutoff,),
        ) as cur:
            out["whale_vs_bot"] = [
                {
                    "signal_id": r[0], "side": r[1],
                    "whale_price": float(r[2] or 0),
                    "simulated_price": float(r[3] or 0),
                    "diff_cents": float(r[4] or 0) * 100,
                    "slippage_pct": float(r[5] or 0) * 100,
                    "status": r[6],
                }
                for r in await cur.fetchall()
            ]
    except aiosqlite.OperationalError:
        pass
    return out


# ============ Trader Attribution ============

async def trader_attribution(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    cutoff = _iso_hours_ago(24)
    try:
        async with conn.execute(
            "SELECT j.wallet_address,"
            " COUNT(*) AS signals,"
            " SUM(CASE WHEN j.status IN ('executed','partial') THEN 1 ELSE 0 END) AS executed,"
            " SUM(CASE WHEN j.status='skipped' THEN 1 ELSE 0 END) AS skipped,"
            " AVG(CASE WHEN j.status IN ('executed','partial') THEN j.slippage END) AS avg_slip,"
            " w.score, w.win_rate, w.pnl_usd, w.name"
            " FROM demo_journal j"
            " LEFT JOIN tracked_wallets w ON LOWER(w.address)=LOWER(j.wallet_address)"
            " WHERE j.timestamp >= ?"
            " GROUP BY j.wallet_address"
            " ORDER BY signals DESC",
            (cutoff,),
        ) as cur:
            rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return []
    # Para cada wallet, busca PnL gerado (positions opened/closed que vieram dela)
    out = []
    for r in rows:
        wallet = r[0]
        signals_total = int(r[1])
        executed_count = int(r[2] or 0)
        skipped_count = int(r[3] or 0)
        avg_slip = float(r[4] or 0)
        score = float(r[5] or 0)
        win_rate_pub = float(r[6] or 0)
        pnl_pub = float(r[7] or 0)
        name = r[8]

        # PnL gerado (closed positions com source_wallets contém este endereço)
        async with conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0),"
            " SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN realized_pnl<0 THEN 1 ELSE 0 END),"
            " MAX(realized_pnl), MIN(realized_pnl)"
            " FROM bot_positions"
            " WHERE is_open=0 AND source_wallets_json LIKE ?",
            (f"%{wallet}%",),
        ) as cur2:
            r2 = await cur2.fetchone()
        demo_pnl = float(r2[0] or 0) if r2 else 0.0
        wins_w = int(r2[1] or 0) if r2 else 0
        losses_w = int(r2[2] or 0) if r2 else 0
        best = float(r2[3] or 0) if r2 else 0.0
        worst = float(r2[4] or 0) if r2 else 0.0

        exec_rate = (
            executed_count / signals_total if signals_total > 0 else 0.0
        )
        # Status visual
        if signals_total < 3:
            verdict = "watch"
        elif demo_pnl > 0 and exec_rate > 0.5:
            verdict = "strong"
        elif demo_pnl < 0:
            verdict = "underperforming"
        else:
            verdict = "watch"

        out.append({
            "wallet": wallet,
            "name": name,
            "score": score,
            "win_rate_public": win_rate_pub,
            "pnl_public": pnl_pub,
            "signals_detected": signals_total,
            "signals_executed": executed_count,
            "signals_skipped": skipped_count,
            "execution_rate": exec_rate,
            "avg_slippage": avg_slip,
            "demo_pnl": demo_pnl,
            "wins": wins_w,
            "losses": losses_w,
            "best_trade": best,
            "worst_trade": worst,
            "verdict": verdict,
        })
    return out


# ============ Risk & Exposure ============

async def risk_exposure(conn: aiosqlite.Connection) -> dict[str, Any]:
    """Exposição por mercado, tag (proxy outcome), trader."""
    out: dict[str, Any] = {"by_market": [], "by_trader": [], "stale_positions": []}
    # Por mercado (condition_id)
    async with conn.execute(
        "SELECT condition_id, market_title, outcome,"
        " SUM(size * COALESCE(current_price, avg_entry_price)) AS market_value,"
        " SUM(size * avg_entry_price) AS invested,"
        " COUNT(*)"
        " FROM bot_positions WHERE is_open=1"
        " GROUP BY condition_id ORDER BY market_value DESC LIMIT 20"
    ) as cur:
        out["by_market"] = [
            {
                "condition_id": r[0],
                "market_title": r[1],
                "outcome": r[2],
                "market_value_usd": float(r[3] or 0),
                "invested_usd": float(r[4] or 0),
                "tokens_count": int(r[5]),
            }
            for r in await cur.fetchall()
        ]
    # Por trader (source_wallets_json)
    async with conn.execute(
        "SELECT source_wallets_json, COUNT(*),"
        " SUM(size * COALESCE(current_price, avg_entry_price))"
        " FROM bot_positions WHERE is_open=1"
        " GROUP BY source_wallets_json"
    ) as cur:
        rows = await cur.fetchall()
    # Achata wallets
    trader_agg: dict[str, dict[str, float]] = {}
    import json as _json
    for r in rows:
        try:
            wallets = _json.loads(r[0]) if r[0] else []
        except Exception:
            wallets = []
        for w in wallets:
            d = trader_agg.setdefault(w, {"positions": 0.0, "exposure": 0.0})
            d["positions"] += float(r[1])
            d["exposure"] += float(r[2] or 0)
    out["by_trader"] = [
        {"wallet": w, "positions": int(d["positions"]), "exposure_usd": d["exposure"]}
        for w, d in sorted(trader_agg.items(), key=lambda x: -x[1]["exposure"])
    ][:20]

    # Posições antigas (>24h)
    cutoff = _iso_hours_ago(24)
    async with conn.execute(
        "SELECT id, market_title, outcome, size, avg_entry_price,"
        " current_price, opened_at"
        " FROM bot_positions WHERE is_open=1 AND opened_at < ?"
        " ORDER BY opened_at LIMIT 20",
        (cutoff,),
    ) as cur:
        out["stale_positions"] = [
            {
                "id": int(r[0]), "market_title": r[1], "outcome": r[2],
                "size": float(r[3] or 0), "entry": float(r[4] or 0),
                "current": float(r[5] or 0) if r[5] else None,
                "opened_at": r[6],
            }
            for r in await cur.fetchall()
        ]
    return out


# ============ Trade Journal (paginado, filtrável) ============

async def trade_journal(
    conn: aiosqlite.Connection, *,
    limit: int = 100,
    side: str | None = None,
    status: str | None = None,
    wallet: str | None = None,
) -> list[dict[str, Any]]:
    where = ["1=1"]
    params: list[Any] = []
    if side:
        where.append("side=?"); params.append(side)
    if status:
        where.append("status=?"); params.append(status)
    if wallet:
        where.append("LOWER(wallet_address)=LOWER(?)"); params.append(wallet)
    sql = (
        f"SELECT timestamp, signal_id, wallet_address, market_title, outcome,"
        f" side, whale_price, simulated_price, spread, slippage,"
        f" intended_size, simulated_size, depth_usd, status, skip_reason,"
        f" is_emergency_exit, source, confluence_count"
        f" FROM demo_journal WHERE {' AND '.join(where)}"
        f" ORDER BY id DESC LIMIT ?"
    )
    params.append(limit)
    try:
        async with conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return []
    return [
        {
            "timestamp": r[0], "signal_id": r[1], "wallet": r[2],
            "market_title": r[3], "outcome": r[4], "side": r[5],
            "whale_price": float(r[6] or 0),
            "simulated_price": float(r[7] or 0),
            "spread": float(r[8] or 0),
            "slippage": float(r[9] or 0),
            "intended_size": float(r[10] or 0),
            "simulated_size": float(r[11] or 0),
            "depth_usd": float(r[12] or 0),
            "status": r[13], "skip_reason": r[14],
            "is_emergency": bool(r[15]),
            "source": r[16],
            "confluence_count": int(r[17] or 1),
        }
        for r in rows
    ]


# ============ Equity curve / time series ============

async def equity_curve(conn: aiosqlite.Connection, *, hours: int = 24) -> list[dict[str, Any]]:
    cutoff = _iso_hours_ago(hours)
    try:
        async with conn.execute(
            "SELECT timestamp, bank_total, total_pnl, current_drawdown_pct,"
            " open_positions, cash_available"
            " FROM demo_bank_snapshots WHERE timestamp >= ?"
            " ORDER BY timestamp",
            (cutoff,),
        ) as cur:
            rows = await cur.fetchall()
    except aiosqlite.OperationalError:
        return []
    return [
        {
            "t": r[0],
            "bank": float(r[1] or 0),
            "pnl": float(r[2] or 0),
            "dd_pct": float(r[3] or 0),
            "positions": int(r[4] or 0),
            "cash": float(r[5] or 0),
        }
        for r in rows
    ]
