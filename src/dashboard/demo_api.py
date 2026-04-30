"""FastAPI sub-app — endpoints da demo dashboard. Plugado em
`src/dashboard/app.py` via `app.include_router(demo_router)`.

Todos protegidos pelo mesmo Basic Auth do dashboard principal.
Retornam JSON pronto pro frontend HUD.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiosqlite
from fastapi import APIRouter, Depends, Query

from src.dashboard.demo_aggregators import (
    equity_curve,
    execution_quality,
    readiness_overview,
    risk_exposure,
    trade_journal,
    trader_attribution,
)
from src.dashboard.readiness_score import compute_readiness


def build_demo_router(
    *,
    auth_dep: Any,
    shared_conn: aiosqlite.Connection,
    starting_bank_usd: float,
    started_at: datetime,
    mode: str,
) -> APIRouter:
    router = APIRouter(prefix="/api/demo", tags=["demo"], dependencies=[auth_dep])

    @router.get("/readiness")
    async def get_readiness() -> dict[str, Any]:
        """Cards principais (banca, PnL, drawdown, win_rate, latências)."""
        data = await readiness_overview(
            shared_conn, starting_bank_usd=starting_bank_usd,
        )
        uptime = (datetime.now(timezone.utc) - started_at).total_seconds()
        data["uptime_seconds"] = uptime
        data["mode"] = mode
        return data

    @router.get("/execution-quality")
    async def get_execution_quality() -> dict[str, Any]:
        return await execution_quality(shared_conn)

    @router.get("/trader-attribution")
    async def get_trader_attribution() -> dict[str, Any]:
        rows = await trader_attribution(shared_conn)
        return {"traders": rows}

    @router.get("/risk-exposure")
    async def get_risk_exposure() -> dict[str, Any]:
        return await risk_exposure(shared_conn)

    @router.get("/journal")
    async def get_journal(
        limit: int = Query(100, ge=1, le=500),
        side: str | None = None,
        status: str | None = None,
        wallet: str | None = None,
    ) -> dict[str, Any]:
        rows = await trade_journal(
            shared_conn, limit=limit, side=side, status=status, wallet=wallet,
        )
        return {"rows": rows, "count": len(rows)}

    @router.get("/equity-curve")
    async def get_equity_curve(hours: int = Query(24, ge=1, le=168)) -> dict[str, Any]:
        rows = await equity_curve(shared_conn, hours=hours)
        return {"points": rows}

    @router.get("/readiness-score")
    async def get_readiness_score() -> dict[str, Any]:
        result = await compute_readiness(
            shared_conn, starting_bank_usd=starting_bank_usd,
        )
        return {
            "score": result.score,
            "recommendation": result.recommendation,
            "components": result.components,
            "no_go_flags": result.no_go_flags,
            "details": result.details,
        }

    @router.get("/summary")
    async def get_summary() -> dict[str, Any]:
        """Tudo de uma vez — usado pelo frontend pra refresh único."""
        readiness = await readiness_overview(
            shared_conn, starting_bank_usd=starting_bank_usd,
        )
        score = await compute_readiness(
            shared_conn, starting_bank_usd=starting_bank_usd,
        )
        return {
            "mode": mode,
            "uptime_seconds": (
                datetime.now(timezone.utc) - started_at
            ).total_seconds(),
            "readiness": readiness,
            "score": {
                "score": score.score,
                "recommendation": score.recommendation,
                "components": score.components,
                "no_go_flags": score.no_go_flags,
                "details": score.details,
            },
        }

    return router
