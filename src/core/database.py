"""SQLite bootstrap, migrations runner, async connection helpers.

WAL mode é obrigatório: o bot roda múltiplas coroutines que leem enquanto o
tracker/executor escrevem. Sem WAL, leitores bloqueiam escritores no SQLite.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "polytrader.db"
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"


def _discover_migrations() -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(path.stem.split("_", 1)[0])
        found.append((version, path))
    return found


async def _applied_versions(db: aiosqlite.Connection) -> set[int]:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    async with db.execute("SELECT version FROM schema_migrations") as cur:
        return {row[0] async for row in cur}


async def init_database(db_path: Path = DEFAULT_DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        # Limita crescimento do .db-wal: checkpoint a cada 500 pages
        # (~2MB). Evita que o WAL file inche em semanas de uptime 24/7.
        await db.execute("PRAGMA wal_autocheckpoint=500;")
        await db.execute("PRAGMA foreign_keys=ON;")
        applied = await _applied_versions(db)
        for version, path in _discover_migrations():
            if version in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            await db.executescript(sql)
            await db.execute(
                "INSERT OR REPLACE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, datetime.now(timezone.utc).isoformat()),
            )
        await db.commit()


@asynccontextmanager
async def get_connection(db_path: Path = DEFAULT_DB_PATH) -> AsyncIterator[aiosqlite.Connection]:
    """Context manager: abre, configura e fecha uma conexão aiosqlite.

    Uso:
        async with get_connection() as db:
            await db.execute(...)
            await db.commit()
    """
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys=ON;")
        yield conn
