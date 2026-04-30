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


async def open_shared_connection(
    db_path: Path = DEFAULT_DB_PATH,
) -> aiosqlite.Connection:
    """Abre a conexão singleton compartilhada pelo processo inteiro.

    Aplica os mesmos pragmas de `get_connection` — hot path writers
    (tracker/executor) herdam synchronous=NORMAL + cache 16MB.

    ⚠️ row_factory = aiosqlite.Row — sem isso, código que acessa
    resultados por nome (`row["realized_pnl"]`) falha silenciosamente
    em workarounds tipo `hasattr(row, "keys")`.
    """
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON;")
    await conn.execute("PRAGMA wal_autocheckpoint=500;")
    await conn.execute("PRAGMA synchronous=NORMAL;")
    await conn.execute("PRAGMA temp_store=MEMORY;")
    await conn.execute("PRAGMA cache_size=-16000;")
    return conn


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
        # wal_autocheckpoint por-conexão — evita .db-wal inchar.
        await conn.execute("PRAGMA wal_autocheckpoint=500;")
        # synchronous=NORMAL: safe em WAL (fsync só no checkpoint),
        # elimina fsync por commit = 2-3× mais rápido em write bursts.
        await conn.execute("PRAGMA synchronous=NORMAL;")
        # Keep-temp-in-memory e cache de 16MB em RAM (era 2MB default).
        await conn.execute("PRAGMA temp_store=MEMORY;")
        await conn.execute("PRAGMA cache_size=-16000;")
        yield conn
