"""Dashboard FastAPI — auth + rotas."""
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
from fastapi.testclient import TestClient

from src.core.database import init_database
from src.core.state import InMemoryState
from src.dashboard.app import build_app
from src.executor.balance_cache import BalanceCache


async def _setup(tmp_path: Path, secret: str = "s3cr3t"):
    db = tmp_path / "d.db"
    await init_database(db)
    conn = await aiosqlite.connect(db)
    state = InMemoryState()

    async def fetcher() -> float:
        return 500.0
    bc = BalanceCache(fetcher)
    await bc.refresh_once()

    app = build_app(
        secret=secret, state=state, balance_cache=bc, shared_conn=conn,
        started_at=datetime.now(timezone.utc), mode="paper",
        vps_location="test", db_path=db,
    )
    return app, conn


async def test_health_no_auth_required(tmp_path: Path):
    app, conn = await _setup(tmp_path)
    try:
        client = TestClient(app)
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["mode"] == "paper"
        assert body["balance_usdc"] == 500.0
    finally:
        await conn.close()


async def test_wallets_requires_auth(tmp_path: Path):
    app, conn = await _setup(tmp_path)
    try:
        client = TestClient(app)
        r = client.get("/wallets")
        assert r.status_code == 401
        r = client.get("/wallets", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401
        r = client.get("/wallets", headers={"Authorization": "Bearer s3cr3t"})
        assert r.status_code == 200
        assert r.json() == []
    finally:
        await conn.close()


async def test_empty_secret_blocks_all_auth_routes(tmp_path: Path):
    app, conn = await _setup(tmp_path, secret="")
    try:
        client = TestClient(app)
        # Health passa
        assert client.get("/health").status_code == 200
        # Wallets bloqueia com 503 (secret não configurado)
        r = client.get("/wallets", headers={"Authorization": "Bearer anything"})
        assert r.status_code == 503
    finally:
        await conn.close()


async def test_signals_and_positions_endpoints(tmp_path: Path):
    app, conn = await _setup(tmp_path)
    try:
        client = TestClient(app)
        headers = {"Authorization": "Bearer s3cr3t"}
        assert client.get("/signals", headers=headers).status_code == 200
        assert client.get("/positions", headers=headers).status_code == 200
        positions_body = client.get("/positions", headers=headers).json()
        assert positions_body["open_count"] == 0
        assert "ram_cache_tokens" in positions_body
    finally:
        await conn.close()
