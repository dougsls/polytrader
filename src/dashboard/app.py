"""Dashboard FastAPI — observabilidade operacional.

Rotas:
    GET /health       — liveness probe, sem auth (systemd / k8s)
    GET /wallets      — pool atual com scores
    GET /signals      — últimos N sinais (inclui skip_reason)
    GET /positions    — bot_positions abertas (inventário do Exit Syncing)

Auth: Bearer token via header `Authorization: Bearer <DASHBOARD_SECRET>`.
A `/health` não exige auth para permitir probe externo. Se o secret for
vazio no `.env`, o server recusa subir em modo `require_auth=true` — evita
deixar o dashboard aberto em nuvem por acidente.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse, PlainTextResponse

from src.core.metrics import halted, render_metrics

from src.core.database import DEFAULT_DB_PATH
from src.core.logger import get_logger
from src.core.state import InMemoryState
from src.executor.balance_cache import BalanceCache
from src.executor.risk_manager import RiskManager

log = get_logger(__name__)


def _auth_dep(secret: str):
    """Factory de dependency — compara bearer token em todas rotas exceto /health."""

    async def _check(authorization: str | None = Header(default=None)) -> None:
        if not secret:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="DASHBOARD_SECRET not configured",
            )
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing Bearer token",
            )
        token = authorization.removeprefix("Bearer ").strip()
        if token != secret:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
            )

    return _check


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
    db_path: Path = DEFAULT_DB_PATH,
) -> FastAPI:
    app = FastAPI(title="polytrader", docs_url=None, redoc_url=None)
    auth = Depends(_auth_dep(secret))

    @app.get("/metrics")
    async def prometheus_metrics() -> PlainTextResponse:
        """Prometheus exposition. Atualiza halt gauge antes de expor."""
        if risk_manager is not None:
            halted.set(1 if risk_manager.is_halted else 0)
        return PlainTextResponse(
            render_metrics(), media_type="text/plain; version=0.0.4",
        )

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

    @app.get("/wallets", dependencies=[auth])
    async def wallets() -> list[dict[str, Any]]:
        async with shared_conn.execute(
            "SELECT address, name, score, pnl_usd, win_rate, total_trades, "
            "       is_active, last_trade_at, updated_at "
            "FROM tracked_wallets ORDER BY score DESC LIMIT 50"
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

    @app.get("/signals", dependencies=[auth])
    async def signals(limit: int = 50) -> list[dict[str, Any]]:
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

    @app.post("/halt", dependencies=[auth])
    async def halt(reason: str = "manual_override") -> dict[str, Any]:
        if risk_manager is None:
            raise HTTPException(status_code=503, detail="risk_manager not wired")
        risk_manager.halt(reason)
        log.warning("manual_halt_via_dashboard", reason=reason)
        return {"halted": True, "reason": reason}

    @app.post("/resume", dependencies=[auth])
    async def resume() -> dict[str, Any]:
        if risk_manager is None:
            raise HTTPException(status_code=503, detail="risk_manager not wired")
        prev_reason = risk_manager.halt_reason
        risk_manager.resume()
        log.warning("manual_resume_via_dashboard", previous_reason=prev_reason)
        return {"halted": False, "previous_reason": prev_reason}

    @app.get("/positions", dependencies=[auth])
    async def positions() -> dict[str, Any]:
        async with shared_conn.execute(
            "SELECT condition_id, token_id, market_title, outcome, size, "
            "       avg_entry_price, current_price, unrealized_pnl, "
            "       source_wallets_json, opened_at "
            "FROM bot_positions WHERE is_open=1 ORDER BY opened_at DESC"
        ) as cur:
            rows = await cur.fetchall()
        return {
            "open_count": len(rows),
            "ram_cache_tokens": len(state.bot_positions_by_token),
            "positions": [
                {
                    "condition_id": r[0], "token_id": r[1],
                    "market_title": r[2], "outcome": r[3],
                    "size": r[4], "avg_entry_price": r[5],
                    "current_price": r[6], "unrealized_pnl": r[7],
                    "source_wallets_json": r[8], "opened_at": r[9],
                }
                for r in rows
            ],
        }

    return app
