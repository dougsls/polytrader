"""POST /halt + /resume + GET /metrics."""
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
from fastapi.testclient import TestClient

from src.core.config import load_yaml_config
from src.core.database import init_database
from src.core.state import InMemoryState
from src.dashboard.app import build_app
from src.executor.balance_cache import BalanceCache
from src.executor.risk_manager import RiskManager


async def _make(tmp_path: Path, with_risk: bool = True):
    db = tmp_path / "d.db"
    await init_database(db)
    conn = await aiosqlite.connect(db)
    state = InMemoryState()

    async def fetcher() -> float:
        return 100.0
    bc = BalanceCache(fetcher)
    await bc.refresh_once()

    rm = RiskManager(load_yaml_config().executor) if with_risk else None
    app = build_app(
        secret="tok", state=state, balance_cache=bc, shared_conn=conn,
        started_at=datetime.now(timezone.utc), mode="live", vps_location="x",
        risk_manager=rm,
    )
    return app, conn, rm


async def test_halt_requires_auth(tmp_path: Path):
    app, conn, _ = await _make(tmp_path)
    try:
        c = TestClient(app)
        assert c.post("/halt").status_code == 401
        assert c.post("/halt", headers={"Authorization": "Bearer wrong"}).status_code == 401
    finally:
        await conn.close()


async def test_halt_triggers_risk_manager(tmp_path: Path):
    app, conn, rm = await _make(tmp_path)
    try:
        c = TestClient(app)
        assert rm.is_halted is False
        r = c.post(
            "/halt?reason=testing",
            headers={"Authorization": "Bearer tok"},
        )
        assert r.status_code == 200
        assert r.json() == {"halted": True, "reason": "testing"}
        assert rm.is_halted is True
        assert rm.halt_reason == "testing"
    finally:
        await conn.close()


async def test_resume_clears_halt(tmp_path: Path):
    app, conn, rm = await _make(tmp_path)
    try:
        rm.halt("previous_failure")
        c = TestClient(app)
        r = c.post("/resume", headers={"Authorization": "Bearer tok"})
        assert r.status_code == 200
        body = r.json()
        assert body["halted"] is False
        assert body["previous_reason"] == "previous_failure"
        assert rm.is_halted is False
    finally:
        await conn.close()


async def test_halt_503_when_risk_manager_missing(tmp_path: Path):
    app, conn, _ = await _make(tmp_path, with_risk=False)
    try:
        c = TestClient(app)
        r = c.post("/halt", headers={"Authorization": "Bearer tok"})
        assert r.status_code == 503
    finally:
        await conn.close()


async def test_metrics_endpoint_exposes_counters(tmp_path: Path):
    app, conn, rm = await _make(tmp_path)
    try:
        c = TestClient(app)
        r = c.get("/metrics")
        assert r.status_code == 200
        body = r.text
        assert "polytrader_signals_received_total" in body
        assert "polytrader_trades_executed_total" in body
        assert "polytrader_errors_total" in body
        assert "polytrader_halted" in body

        # Ao haltar, o gauge muda
        rm.halt("testing")
        body2 = c.get("/metrics").text
        # Procura linha do gauge halted == 1
        assert "polytrader_halted 1" in body2.splitlines()[-10:].__str__() or \
            any("polytrader_halted 1" in ln for ln in body2.splitlines())
    finally:
        await conn.close()
