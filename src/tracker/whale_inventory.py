"""Sincroniza `whale_inventory` via polling de /positions (Regra 2).

Estratégia: a cada N segundos, puxar `/positions?user={addr}` de cada
carteira rastreada e fazer UPSERT na tabela. O signal_detector consulta
este snapshot para decidir o tamanho proporcional de um SELL.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.api.data_client import DataAPIClient
from src.core.database import DEFAULT_DB_PATH, get_connection
from src.core.logger import get_logger
from src.core.state import InMemoryState

log = get_logger(__name__)


async def snapshot_whale(
    data_client: DataAPIClient,
    wallet_address: str,
    *,
    db_path: Path = DEFAULT_DB_PATH,
    state: InMemoryState | None = None,
) -> list[dict[str, Any]]:
    """Pull + persistência do inventário de uma baleia.

    Faz write-through no `state` (Fase 4) para que o fast-path do
    `detect_signal` veja o snapshot em < 1μs.
    """
    positions = await data_client.positions(wallet_address)
    now = datetime.now(timezone.utc).isoformat()

    async with get_connection(db_path) as db:
        for pos in positions:
            token_id = pos.get("asset") or pos.get("tokenId") or pos.get("token_id")
            condition_id = pos.get("conditionId") or pos.get("condition_id")
            size = float(pos.get("size") or pos.get("tokens") or 0)
            avg_price = pos.get("avgPrice") or pos.get("avg_price")
            if not token_id or size <= 0:
                continue
            if state is not None:
                state.whale_set(wallet_address, token_id, size)
            await db.execute(
                """
                INSERT INTO whale_inventory
                    (wallet_address, condition_id, token_id, size, avg_price, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(wallet_address, token_id) DO UPDATE SET
                    size=excluded.size,
                    avg_price=excluded.avg_price,
                    last_seen_at=excluded.last_seen_at,
                    condition_id=excluded.condition_id
                """,
                (wallet_address, condition_id, token_id, size,
                 float(avg_price) if avg_price is not None else None, now),
            )
        await db.commit()
    log.info("whale_snapshot", address=wallet_address, positions=len(positions))
    return positions


async def get_whale_size(
    wallet_address: str,
    token_id: str,
    *,
    db_path: Path = DEFAULT_DB_PATH,
) -> float:
    """Último tamanho conhecido da baleia naquele token. 0 se não houver."""
    async with get_connection(db_path) as db:
        async with db.execute(
            "SELECT size FROM whale_inventory WHERE wallet_address=? AND token_id=?",
            (wallet_address, token_id),
        ) as cur:
            row = await cur.fetchone()
    return float(row["size"]) if row else 0.0
