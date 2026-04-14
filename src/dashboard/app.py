"""Dashboard FastAPI — UI HTML + API JSON com Basic Auth.

Rotas:
    GET  /              — Single-page HTML com auto-refresh via JS fetch
    GET  /health        — liveness probe, sem auth
    GET  /metrics       — Prometheus exposition, sem auth
    GET  /api/overview  — resumo agregado pra UI
    GET  /api/wallets   — pool de whales + scores
    GET  /api/signals   — últimos sinais (com skip_reason)
    GET  /api/positions — bot_positions abertas
    GET  /api/trades    — copy_trades executados (paper fills)
    POST /halt          — trava global
    POST /resume        — destrava

Auth: HTTP Basic (user/password do .env). Browser prompt nativo.
Bearer Authorization header também aceito pra programmatic access.
"""
from __future__ import annotations

import base64
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from src.core.database import DEFAULT_DB_PATH
from src.core.logger import get_logger
from src.core.metrics import halted, render_metrics
from src.core.state import InMemoryState
from src.executor.balance_cache import BalanceCache
from src.executor.risk_manager import RiskManager

log = get_logger(__name__)

DASHBOARD_HTML_PATH = Path(__file__).parent / "templates" / "index.html"


def aiosqlite_safe_loads(raw: str | None) -> list[str]:
    """Parse JSON array tolerante a null/malformed."""
    if not raw:
        return []
    try:
        import json as _json
        v = _json.loads(raw)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _auth_dep(username: str, password: str):
    """Accepts HTTP Basic (user:pass) OR Bearer <password>."""

    async def _check(authorization: str | None = Header(default=None)) -> None:
        if not password:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="DASHBOARD_SECRET not configured",
            )
        if not authorization:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Auth required",
                headers={"WWW-Authenticate": 'Basic realm="polytrader"'},
            )
        if authorization.startswith("Basic "):
            try:
                decoded = base64.b64decode(
                    authorization.removeprefix("Basic ").strip()
                ).decode("utf-8")
                user_in, _, pass_in = decoded.partition(":")
            except Exception as exc:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Malformed Basic auth",
                    headers={"WWW-Authenticate": 'Basic realm="polytrader"'},
                ) from exc
            if not (
                secrets.compare_digest(user_in, username)
                and secrets.compare_digest(pass_in, password)
            ):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid credentials",
                    headers={"WWW-Authenticate": 'Basic realm="polytrader"'},
                )
            return
        if authorization.startswith("Bearer "):
            token = authorization.removeprefix("Bearer ").strip()
            if not secrets.compare_digest(token, password):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid token",
                )
            return
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unsupported auth scheme",
            headers={"WWW-Authenticate": 'Basic realm="polytrader"'},
        )

    return _check


async def _compute_pnl(conn: aiosqlite.Connection) -> dict[str, float]:
    """Realized + unrealized PnL agregados."""
    async with conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0), COALESCE(SUM(unrealized_pnl), 0) "
        "FROM bot_positions"
    ) as cur:
        row = await cur.fetchone()
    realized = float(row[0]) if row else 0.0
    unrealized = float(row[1]) if row else 0.0
    day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    async with conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0) FROM bot_positions WHERE closed_at >= ?",
        (day_ago,),
    ) as cur:
        row = await cur.fetchone()
    realized_24h = float(row[0]) if row else 0.0
    return {
        "realized_total": realized,
        "realized_24h": realized_24h,
        "unrealized": unrealized,
        "total": realized + unrealized,
    }


async def _signal_counts(conn: aiosqlite.Connection) -> dict[str, int]:
    day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    async with conn.execute(
        "SELECT status, COUNT(*) FROM trade_signals WHERE detected_at >= ? GROUP BY status",
        (day_ago,),
    ) as cur:
        return {row[0]: int(row[1]) for row in await cur.fetchall()}


def build_app(
    *,
    secret: str,
    state: InMemoryState,
    balance_cache: BalanceCache,
    shared_conn: aiosqlite.Connection,
    started_at: datetime,
    mode: str,
    vps_location: str,
    risk_manager: RiskManager | None = None,
    dashboard_user: str = "operator",
    db_path: Path = DEFAULT_DB_PATH,
) -> FastAPI:
    app = FastAPI(title="polytrader", docs_url=None, redoc_url=None)
    auth = Depends(_auth_dep(dashboard_user, secret))

    # --------- public (no auth) ---------

    @app.get("/health")
    async def health() -> JSONResponse:
        uptime = (datetime.now(timezone.utc) - started_at).total_seconds()
        return JSONResponse({
            "status": "ok",
            "mode": mode,
            "vps_location": vps_location,
            "uptime_seconds": round(uptime, 1),
            "balance_fresh": balance_cache.is_fresh,
            "balance_usdc": balance_cache.balance_usdc,
            "bot_tokens_tracked": len(state.bot_positions_by_token),
            "whale_inventory_entries": len(state.whale_inventory),
        })

    @app.get("/metrics")
    async def prometheus_metrics() -> PlainTextResponse:
        if risk_manager is not None:
            halted.set(1 if risk_manager.is_halted else 0)
        return PlainTextResponse(
            render_metrics(), media_type="text/plain; version=0.0.4",
        )

    # --------- UI: HTML single-page ---------

    @app.get("/", response_class=HTMLResponse, dependencies=[auth])
    async def index() -> HTMLResponse:
        html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
        return HTMLResponse(html)

    # --------- API protected ---------

    @app.get("/api/overview", dependencies=[auth])
    async def api_overview() -> dict[str, Any]:
        uptime = (datetime.now(timezone.utc) - started_at).total_seconds()
        pnl = await _compute_pnl(shared_conn)
        signal_counts = await _signal_counts(shared_conn)
        async with shared_conn.execute(
            "SELECT COUNT(*) FROM bot_positions WHERE is_open=1"
        ) as cur:
            open_positions = int((await cur.fetchone())[0])
        async with shared_conn.execute(
            "SELECT COUNT(*) FROM tracked_wallets WHERE is_active=1"
        ) as cur:
            active_whales = int((await cur.fetchone())[0])
        # Capital alocado em posições abertas (custo de aquisição) +
        # valor atual de mercado dessas posições. Permite ver banca dinâmica.
        async with shared_conn.execute(
            "SELECT COALESCE(SUM(size * avg_entry_price), 0), "
            "       COALESCE(SUM(size * COALESCE(current_price, avg_entry_price)), 0) "
            "FROM bot_positions WHERE is_open=1"
        ) as cur:
            row = await cur.fetchone()
        invested_in_positions = float(row[0]) if row else 0.0
        market_value_positions = float(row[1]) if row else 0.0
        # Placar por categoria de fechamento (só 'resolved' conta ganho/perda real)
        async with shared_conn.execute(
            "SELECT close_reason, "
            "       SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) AS wins, "
            "       SUM(CASE WHEN realized_pnl<0 THEN 1 ELSE 0 END) AS losses, "
            "       COALESCE(SUM(realized_pnl), 0) AS pnl, COUNT(*) AS n "
            "FROM bot_positions WHERE is_open=0 GROUP BY close_reason"
        ) as cur:
            scoreboard_rows = await cur.fetchall()
        scoreboard = {"resolved": {"wins":0,"losses":0,"pnl":0.0,"n":0},
                      "sold": {"wins":0,"losses":0,"pnl":0.0,"n":0},
                      "legacy": {"wins":0,"losses":0,"pnl":0.0,"n":0}}
        for sr in scoreboard_rows:
            key = sr[0] if sr[0] in ("resolved","sold") else "legacy"
            scoreboard[key] = {"wins": int(sr[1] or 0), "losses": int(sr[2] or 0),
                               "pnl": float(sr[3] or 0.0), "n": int(sr[4] or 0)}
        # Saldo "demo" dinâmico: starting bank + realized PnL - capital travado em posições
        starting_bank = balance_cache.balance_usdc  # paper: max_portfolio_usd
        cash_available = starting_bank + pnl["realized_total"] - invested_in_positions
        portfolio_total = cash_available + market_value_positions

        return {
            "mode": mode,
            "vps_location": vps_location,
            "uptime_seconds": round(uptime, 1),
            "balance_usdc": balance_cache.balance_usdc,  # back-compat
            "balance_fresh": balance_cache.is_fresh,
            "starting_bank": starting_bank,
            "cash_available": cash_available,
            "invested_in_positions": invested_in_positions,
            "market_value_positions": market_value_positions,
            "portfolio_total": portfolio_total,
            "halted": risk_manager.is_halted if risk_manager else False,
            "halt_reason": risk_manager.halt_reason if risk_manager else None,
            "active_whales": active_whales,
            "open_positions": open_positions,
            "whale_inventory_entries": len(state.whale_inventory),
            "pnl": pnl,
            "signals_24h": signal_counts,
            "scoreboard": scoreboard,
        }

    @app.get("/api/equity-curve", dependencies=[auth])
    async def api_equity_curve(hours: int = 24) -> dict[str, Any]:
        """Reconstrói curva da banca: starting_bank + soma cumulativa de
        realized_pnl ordenada por closed_at. Cada ponto é um fechamento.
        Retorna também o snapshot atual (banca = start + realized + unrealized)."""
        hours = max(1, min(hours, 168))  # 1h a 7d
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        starting_bank = balance_cache.balance_usdc
        async with shared_conn.execute(
            "SELECT closed_at, realized_pnl FROM bot_positions "
            "WHERE is_open=0 AND closed_at >= ? ORDER BY closed_at ASC",
            (cutoff,),
        ) as cur:
            rows = await cur.fetchall()
        points: list[dict[str, Any]] = []
        cumulative = 0.0
        # Ponto inicial: banca no cutoff
        points.append({"t": cutoff, "v": starting_bank, "pnl": 0.0})
        for r in rows:
            cumulative += float(r[1] or 0.0)
            points.append({
                "t": r[0],
                "v": starting_bank + cumulative,
                "pnl": cumulative,
            })
        # Ponto final = agora com unrealized incluído
        async with shared_conn.execute(
            "SELECT COALESCE(SUM(unrealized_pnl), 0) FROM bot_positions WHERE is_open=1"
        ) as cur:
            row = await cur.fetchone()
        unrealized = float(row[0]) if row else 0.0
        now_iso = datetime.now(timezone.utc).isoformat()
        points.append({
            "t": now_iso,
            "v": starting_bank + cumulative + unrealized,
            "pnl": cumulative + unrealized,
        })
        return {"starting_bank": starting_bank, "points": points,
                "current_value": starting_bank + cumulative + unrealized}

    @app.get("/api/velocity", dependencies=[auth])
    async def api_velocity() -> dict[str, Any]:
        """Lucro por minuto/hora/dia, com comparativo do período anterior."""
        now = datetime.now(timezone.utc)
        async def pnl_in_window(start: datetime, end: datetime) -> float:
            async with shared_conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) FROM bot_positions "
                "WHERE is_open=0 AND closed_at >= ? AND closed_at < ?",
                (start.isoformat(), end.isoformat()),
            ) as cur:
                r = await cur.fetchone()
            return float(r[0]) if r else 0.0
        # Janelas: última hora vs hora anterior; hoje vs ontem; etc.
        h1 = await pnl_in_window(now - timedelta(hours=1), now)
        h2 = await pnl_in_window(now - timedelta(hours=2), now - timedelta(hours=1))
        d1 = await pnl_in_window(now - timedelta(hours=24), now)
        d2 = await pnl_in_window(now - timedelta(hours=48), now - timedelta(hours=24))
        m5 = await pnl_in_window(now - timedelta(minutes=5), now)
        m10 = await pnl_in_window(now - timedelta(minutes=10), now - timedelta(minutes=5))
        return {
            "per_minute": m5 / 5.0,
            "per_minute_prev": m10 / 5.0,
            "per_hour": h1,
            "per_hour_prev": h2,
            "per_day": d1,
            "per_day_prev": d2,
        }

    @app.get("/api/insights", dependencies=[auth])
    async def api_insights() -> dict[str, Any]:
        """Best hour, best market type, current streak, biggest win/loss."""
        # Best hour (last 7d)
        async with shared_conn.execute(
            "SELECT strftime('%H', closed_at) AS h, "
            "       SUM(realized_pnl) AS pnl, COUNT(*) AS n "
            "FROM bot_positions WHERE is_open=0 AND closed_at IS NOT NULL "
            "AND realized_pnl IS NOT NULL "
            "GROUP BY h ORDER BY pnl DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        best_hour = ({"hour": int(row[0]), "pnl": float(row[1]), "n": int(row[2])}
                     if row and row[0] else None)
        # Streak atual (resolvidas em ordem reversa)
        async with shared_conn.execute(
            "SELECT realized_pnl FROM bot_positions "
            "WHERE is_open=0 AND close_reason='resolved' "
            "ORDER BY closed_at DESC LIMIT 50"
        ) as cur:
            recent = [float(r[0] or 0) for r in await cur.fetchall()]
        streak_count = 0
        streak_kind = None
        for pnl in recent:
            if pnl > 0:
                if streak_kind in (None, "win"):
                    streak_kind = "win"; streak_count += 1
                else: break
            elif pnl < 0:
                if streak_kind in (None, "loss"):
                    streak_kind = "loss"; streak_count += 1
                else: break
        # Biggest win / biggest loss (24h)
        day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        async with shared_conn.execute(
            "SELECT market_title, realized_pnl FROM bot_positions "
            "WHERE is_open=0 AND closed_at >= ? "
            "ORDER BY realized_pnl DESC LIMIT 1", (day_ago,),
        ) as cur:
            r = await cur.fetchone()
        biggest_win = ({"market": r[0], "pnl": float(r[1])}
                       if r and float(r[1] or 0) > 0 else None)
        async with shared_conn.execute(
            "SELECT market_title, realized_pnl FROM bot_positions "
            "WHERE is_open=0 AND closed_at >= ? "
            "ORDER BY realized_pnl ASC LIMIT 1", (day_ago,),
        ) as cur:
            r = await cur.fetchone()
        biggest_loss = ({"market": r[0], "pnl": float(r[1])}
                        if r and float(r[1] or 0) < 0 else None)
        # Best whale (PnL gerado nos últimos 7d, attribution via source_wallets_json)
        async with shared_conn.execute(
            "SELECT source_wallets_json, realized_pnl FROM bot_positions "
            "WHERE is_open=0 AND closed_at >= datetime('now','-7 days') "
            "AND realized_pnl != 0"
        ) as cur:
            attr_rows = await cur.fetchall()
        whale_pnl: dict[str, float] = {}
        for row in attr_rows:
            try:
                wallets = aiosqlite_safe_loads(row[0])
            except Exception:
                wallets = []
            if wallets:
                share = float(row[1]) / len(wallets)
                for w in wallets:
                    whale_pnl[w.lower()] = whale_pnl.get(w.lower(), 0.0) + share
        best_whale = None
        if whale_pnl:
            best_addr = max(whale_pnl, key=whale_pnl.get)
            async with shared_conn.execute(
                "SELECT name FROM tracked_wallets WHERE LOWER(address)=?",
                (best_addr,),
            ) as cur:
                wn = await cur.fetchone()
            best_whale = {
                "address": best_addr,
                "name": wn[0] if wn else best_addr[:10],
                "pnl": whale_pnl[best_addr],
            }
        return {
            "best_hour": best_hour,
            "streak": {"kind": streak_kind, "count": streak_count},
            "biggest_win": biggest_win,
            "biggest_loss": biggest_loss,
            "best_whale": best_whale,
        }

    @app.get("/api/position/{pid}", dependencies=[auth])
    async def api_position_detail(pid: int) -> dict[str, Any]:
        """Timeline completa de uma posição: signal → trade → fill → resolução."""
        async with shared_conn.execute(
            "SELECT id, condition_id, token_id, market_title, outcome, size, "
            "       avg_entry_price, current_price, unrealized_pnl, realized_pnl, "
            "       is_open, source_wallets_json, opened_at, closed_at, close_reason "
            "FROM bot_positions WHERE id=?", (pid,),
        ) as cur:
            p = await cur.fetchone()
        if not p:
            raise HTTPException(404, "posição não encontrada")
        position = {
            "id": p[0], "condition_id": p[1], "token_id": p[2],
            "market_title": p[3], "outcome": p[4], "size": p[5],
            "avg_entry_price": p[6], "current_price": p[7],
            "unrealized_pnl": p[8], "realized_pnl": p[9],
            "is_open": bool(p[10]),
            "source_wallets": aiosqlite_safe_loads(p[11]),
            "opened_at": p[12], "closed_at": p[13],
            "close_reason": p[14],
        }
        # Copy trades desta posição (via token_id)
        async with shared_conn.execute(
            "SELECT id, signal_id, side, intended_size, executed_size, "
            "       intended_price, executed_price, slippage, status, "
            "       created_at, filled_at "
            "FROM copy_trades WHERE token_id=? ORDER BY created_at ASC", (p[2],),
        ) as cur:
            trades = [
                {"id": t[0], "signal_id": t[1], "side": t[2],
                 "intended_size": t[3], "executed_size": t[4],
                 "intended_price": t[5], "executed_price": t[6],
                 "slippage": t[7], "status": t[8],
                 "created_at": t[9], "filled_at": t[10]}
                for t in await cur.fetchall()
            ]
        # Market metadata
        async with shared_conn.execute(
            "SELECT end_date, slug FROM market_metadata_cache WHERE condition_id=?",
            (p[1],),
        ) as cur:
            mkt = await cur.fetchone()
        return {
            "position": position,
            "trades": trades,
            "market": {"end_date": mkt[0] if mkt else None,
                       "slug": mkt[1] if mkt else None},
        }

    @app.get("/api/analytics/histogram", dependencies=[auth])
    async def api_hist() -> dict[str, Any]:
        """Distribuição de PnL em bins de $2. Útil pra ver se lucros/prejuízos
        são concentrados ou dispersos."""
        async with shared_conn.execute(
            "SELECT realized_pnl FROM bot_positions "
            "WHERE is_open=0 AND realized_pnl IS NOT NULL"
        ) as cur:
            pnls = [float(r[0] or 0.0) for r in await cur.fetchall()]
        if not pnls:
            return {"bins": [], "min": 0, "max": 0, "count": 0}
        bin_size = 2.0
        lo = min(pnls); hi = max(pnls)
        # Bins alinhados a múltiplos de bin_size
        bin_lo = (int(lo / bin_size) - 1) * bin_size
        bin_hi = (int(hi / bin_size) + 1) * bin_size
        n_bins = max(1, int((bin_hi - bin_lo) / bin_size))
        counts = [0] * n_bins
        for p in pnls:
            idx = min(n_bins - 1, max(0, int((p - bin_lo) / bin_size)))
            counts[idx] += 1
        bins = [{"lo": bin_lo + i * bin_size, "hi": bin_lo + (i+1) * bin_size,
                 "count": counts[i]} for i in range(n_bins)]
        return {"bins": bins, "min": lo, "max": hi, "count": len(pnls),
                "bin_size": bin_size}

    @app.get("/api/analytics/whales", dependencies=[auth])
    async def api_whales_perf() -> list[dict[str, Any]]:
        """Leaderboard de whales por PnL GERADO pro nosso bot (attribution
        via source_wallets_json). Diferente do /api/wallets que mostra stats
        públicas da whale na Polymarket."""
        async with shared_conn.execute(
            "SELECT source_wallets_json, realized_pnl, is_open, unrealized_pnl "
            "FROM bot_positions WHERE source_wallets_json IS NOT NULL"
        ) as cur:
            rows = await cur.fetchall()
        agg: dict[str, dict[str, Any]] = {}
        for r in rows:
            wallets = aiosqlite_safe_loads(r[0])
            if not wallets:
                continue
            pnl = float(r[1] or 0.0) + (float(r[3] or 0.0) if r[2] else 0.0)
            share = 1.0 / len(wallets)
            for w in wallets:
                addr = w.lower()
                d = agg.setdefault(addr, {
                    "address": addr, "pnl": 0.0, "n": 0, "wins": 0, "losses": 0,
                })
                d["pnl"] += pnl * share
                d["n"] += 1
                if not r[2]:  # closed
                    if float(r[1] or 0.0) > 0:
                        d["wins"] += 1
                    elif float(r[1] or 0.0) < 0:
                        d["losses"] += 1
        # Enrich com names
        names: dict[str, str] = {}
        async with shared_conn.execute(
            "SELECT LOWER(address), name FROM tracked_wallets"
        ) as cur:
            for row in await cur.fetchall():
                names[row[0]] = row[1]
        out = []
        for addr, d in agg.items():
            d["name"] = names.get(addr, addr[:8] + "…" + addr[-4:])
            resolved = d["wins"] + d["losses"]
            d["wr"] = (d["wins"] / resolved * 100.0) if resolved > 0 else None
            out.append(d)
        out.sort(key=lambda x: x["pnl"], reverse=True)
        return out

    @app.get("/api/analytics/heatmap", dependencies=[auth])
    async def api_heatmap() -> dict[str, Any]:
        """Grid 7 (dia da semana) × 24 (hora) com soma de PnL por célula."""
        async with shared_conn.execute(
            "SELECT closed_at, realized_pnl FROM bot_positions "
            "WHERE is_open=0 AND closed_at IS NOT NULL"
        ) as cur:
            rows = await cur.fetchall()
        grid = [[0.0] * 24 for _ in range(7)]  # [dow][hour]
        counts = [[0] * 24 for _ in range(7)]
        for r in rows:
            try:
                d = datetime.fromisoformat((r[0] or "").replace("Z", "+00:00"))
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                dow = d.weekday()  # 0=Mon
                hr = d.hour
                grid[dow][hr] += float(r[1] or 0.0)
                counts[dow][hr] += 1
            except Exception:
                continue
        return {"grid": grid, "counts": counts}

    @app.get("/api/wallets", dependencies=[auth])
    async def api_wallets() -> list[dict[str, Any]]:
        async with shared_conn.execute(
            "SELECT address, name, score, pnl_usd, win_rate, total_trades, "
            "       is_active, last_trade_at, updated_at "
            "FROM tracked_wallets ORDER BY is_active DESC, score DESC LIMIT 50"
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "address": r[0], "name": r[1], "score": r[2],
                "pnl_usd": r[3], "win_rate": r[4], "total_trades": r[5],
                "is_active": bool(r[6]), "last_trade_at": r[7],
                "updated_at": r[8],
            }
            for r in rows
        ]

    @app.get("/api/signals", dependencies=[auth])
    async def api_signals(limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        async with shared_conn.execute(
            "SELECT id, wallet_address, condition_id, token_id, side, size, "
            "       price, usd_value, market_title, hours_to_resolution, "
            "       detected_at, status, skip_reason "
            "FROM trade_signals ORDER BY detected_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "id": r[0], "wallet": r[1], "condition_id": r[2],
                "token_id": r[3], "side": r[4], "size": r[5],
                "price": r[6], "usd_value": r[7], "market_title": r[8],
                "hours_to_resolution": r[9], "detected_at": r[10],
                "status": r[11], "skip_reason": r[12],
            }
            for r in rows
        ]

    @app.get("/api/positions", dependencies=[auth])
    async def api_positions() -> dict[str, Any]:
        # Abertas + últimas 50 fechadas, join com market cache pra end_date.
        sql = (
            "SELECT bp.id, bp.condition_id, bp.token_id, bp.market_title, "
            "       bp.outcome, bp.size, bp.avg_entry_price, bp.current_price, "
            "       bp.unrealized_pnl, bp.realized_pnl, bp.is_open, "
            "       bp.source_wallets_json, bp.opened_at, bp.closed_at, "
            "       m.end_date, bp.close_reason "
            "FROM bot_positions bp "
            "LEFT JOIN market_metadata_cache m ON m.condition_id = bp.condition_id "
            "WHERE bp.is_open=1 "
            "   OR (bp.is_open=0 AND bp.closed_at >= datetime('now', '-30 days')) "
            "ORDER BY bp.is_open DESC, COALESCE(bp.closed_at, bp.opened_at) DESC "
            "LIMIT 500"
        )
        async with shared_conn.execute(sql) as cur:
            rows = await cur.fetchall()
        now = datetime.now(timezone.utc)
        open_list, closed_list = [], []
        for r in rows:
            end_date_raw = r[14]
            hours_left = None
            if end_date_raw:
                try:
                    ed = datetime.fromisoformat(end_date_raw.replace("Z", "+00:00"))
                    if ed.tzinfo is None:
                        ed = ed.replace(tzinfo=timezone.utc)
                    hours_left = (ed - now).total_seconds() / 3600.0
                except (ValueError, AttributeError):
                    pass
            entry = float(r[6]) if r[6] else 0
            current = float(r[7]) if r[7] else entry
            pct_pnl = (current - entry) / entry * 100 if entry > 0 else 0.0
            is_open = bool(r[10])
            realized = float(r[9]) if r[9] else 0.0
            close_reason = r[15]  # 'sold' | 'resolved' | None (legacy)
            outcome_result = None
            if not is_open:
                if close_reason == "resolved":
                    outcome_result = "won" if realized > 0 else "lost"
                elif close_reason == "sold":
                    outcome_result = "sold"  # whale vendeu, bot copiou
                else:
                    # Legacy positions sem close_reason: inferir por PnL
                    outcome_result = "won" if realized > 0 else ("lost" if realized < 0 else "sold")
            item = {
                "id": r[0], "condition_id": r[1], "token_id": r[2],
                "market_title": r[3], "outcome": r[4],
                "size": r[5], "avg_entry_price": r[6],
                "current_price": r[7], "unrealized_pnl": r[8],
                "realized_pnl": realized, "pct_pnl": pct_pnl,
                "is_open": is_open, "outcome_result": outcome_result,
                "close_reason": close_reason,
                "source_wallets_json": r[11],
                "opened_at": r[12], "closed_at": r[13],
                "end_date": end_date_raw, "hours_to_resolution": hours_left,
            }
            if is_open:
                open_list.append(item)
            else:
                closed_list.append(item)
        return {
            "open_count": len(open_list),
            "closed_count": len(closed_list),
            "ram_cache_tokens": len(state.bot_positions_by_token),
            "positions": open_list,          # back-compat
            "open": open_list,
            "closed": closed_list,
        }

    @app.get("/api/trades", dependencies=[auth])
    async def api_trades(limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        async with shared_conn.execute(
            "SELECT c.id, c.signal_id, c.condition_id, c.token_id, c.side, "
            "       c.intended_size, c.executed_size, c.intended_price, "
            "       c.executed_price, c.slippage, c.status, c.created_at, "
            "       c.filled_at, c.error, s.market_title "
            "FROM copy_trades c LEFT JOIN trade_signals s ON s.id = c.signal_id "
            "ORDER BY c.created_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "id": r[0], "signal_id": r[1], "condition_id": r[2],
                "token_id": r[3], "side": r[4],
                "intended_size": r[5], "executed_size": r[6],
                "intended_price": r[7], "executed_price": r[8],
                "slippage": r[9], "status": r[10],
                "created_at": r[11], "filled_at": r[12],
                "error": r[13], "market_title": r[14],
            }
            for r in rows
        ]

    # --------- Governance ---------

    @app.post("/halt", dependencies=[auth])
    async def halt_endpoint(reason: str = "manual_override") -> dict[str, Any]:
        if risk_manager is None:
            raise HTTPException(status_code=503, detail="risk_manager not wired")
        risk_manager.halt(reason)
        log.warning("manual_halt_via_dashboard", reason=reason)
        return {"halted": True, "reason": reason}

    @app.post("/resume", dependencies=[auth])
    async def resume_endpoint() -> dict[str, Any]:
        if risk_manager is None:
            raise HTTPException(status_code=503, detail="risk_manager not wired")
        prev_reason = risk_manager.halt_reason
        risk_manager.resume()
        log.warning("manual_resume_via_dashboard", previous_reason=prev_reason)
        return {"halted": False, "previous_reason": prev_reason}

    # --------- Back-compat (rotas v1 sem /api) ---------

    @app.get("/wallets", dependencies=[auth])
    async def wallets_legacy() -> list[dict[str, Any]]:
        return await api_wallets()

    @app.get("/signals", dependencies=[auth])
    async def signals_legacy(limit: int = 50) -> list[dict[str, Any]]:
        return await api_signals(limit=limit)

    @app.get("/positions", dependencies=[auth])
    async def positions_legacy() -> dict[str, Any]:
        return await api_positions()

    return app
