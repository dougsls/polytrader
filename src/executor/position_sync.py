"""Orphan recovery + bot inventory auto-sync (Fase 5+).

Cenário coberto: a VPS reinicia no meio de um `apply_fill` (entre o
UPDATE no DB e o commit) ou crasha logo após enviar uma ordem que
preencheu mas cujo update local nunca aconteceu. Nesse caso:
    - `bot_positions` no SQLite está defasado
    - `InMemoryState` que carrega do SQLite herda a defasagem
    - a Regra 2 (Exit Syncing) decide com base em dado errado

Solução: no **BOOT**, ANTES de abrir qualquer WebSocket, puxar
`/positions?user={funder}` da Data API da Polymarket (estado canônico
on-chain) e reconciliar com o DB local. Qualquer divergência é logada
como orphan_detected + corrigida.

Função: `reconcile_bot_positions(...)`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import orjson

from src.api.data_client import DataAPIClient
from src.core.database import DEFAULT_DB_PATH, get_connection
from src.core.logger import get_logger
from src.core.state import InMemoryState

log = get_logger(__name__)


def _normalize(pos: dict[str, Any]) -> tuple[str | None, str | None, float, str, float]:
    """Extrai (token_id, condition_id, size, outcome, avg_price) do payload."""
    tok = pos.get("asset") or pos.get("tokenId") or pos.get("token_id")
    cid = pos.get("conditionId") or pos.get("condition_id")
    size = float(pos.get("size") or pos.get("tokens") or pos.get("currentSize") or 0)
    outcome = pos.get("outcome") or pos.get("side") or ""
    avg = float(pos.get("avgPrice") or pos.get("avg_price") or pos.get("price") or 0)
    return tok, cid, size, outcome, avg


async def reconcile_bot_positions(
    *,
    data_client: DataAPIClient,
    wallet_address: str,
    conn: aiosqlite.Connection | None = None,
    state: InMemoryState | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, int]:
    """Sincroniza bot_positions + state cache com o on-chain.

    Returns:
        Contadores `{added, updated, closed, ignored}` — o caller loga.
    """
    if not wallet_address:
        log.warning("position_sync_skipped", reason="no_wallet_address")
        return {"added": 0, "updated": 0, "closed": 0, "ignored": 0}

    log.info("position_sync_start", wallet=wallet_address[:12] + "…")
    onchain = await data_client.positions(wallet_address)
    now = datetime.now(timezone.utc).isoformat()

    stats = {"added": 0, "updated": 0, "closed": 0, "ignored": 0}
    onchain_tokens: set[str] = set()

    async def _apply(db: aiosqlite.Connection) -> None:
        # 1. UPSERT cada posição on-chain
        for pos in onchain:
            tok, cid, size, outcome, avg = _normalize(pos)
            if not tok or size <= 0:
                stats["ignored"] += 1
                continue
            onchain_tokens.add(tok)

            async with db.execute(
                "SELECT id, size FROM bot_positions "
                "WHERE token_id=? AND is_open=1",
                (tok,),
            ) as cur:
                existing = await cur.fetchone()

            if existing is None:
                # Orphan: posição existe on-chain mas não no DB → recuperar
                log.warning(
                    "orphan_detected_adding",
                    token_id=tok, size=size, avg_price=avg,
                )
                await db.execute(
                    """
                    INSERT INTO bot_positions
                        (condition_id, token_id, market_title, outcome, size,
                         avg_entry_price, current_price, source_wallets_json,
                         opened_at, is_open, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        cid or "", tok, "", outcome, size, avg, avg,
                        orjson.dumps([]).decode(), now, now,
                    ),
                )
                stats["added"] += 1
            else:
                db_size = float(existing[1])
                if abs(db_size - size) > 1e-6:
                    log.warning(
                        "orphan_detected_resize",
                        token_id=tok, db_size=db_size, onchain_size=size,
                    )
                    await db.execute(
                        "UPDATE bot_positions SET size=?, current_price=?, "
                        "updated_at=? WHERE id=?",
                        (size, avg, now, existing[0]),
                    )
                    stats["updated"] += 1

        # 2. Fechar posições "fantasma": abertas no DB mas não on-chain
        async with db.execute(
            "SELECT id, token_id, size FROM bot_positions WHERE is_open=1"
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            tok = row[1] if not hasattr(row, "keys") else row["token_id"]
            if tok not in onchain_tokens:
                log.warning("phantom_position_closed", token_id=tok)
                pid = row[0] if not hasattr(row, "keys") else row["id"]
                await db.execute(
                    "UPDATE bot_positions SET size=0, is_open=0, "
                    "closed_at=?, updated_at=? WHERE id=?",
                    (now, now, pid),
                )
                stats["closed"] += 1

        await db.commit()

    if conn is not None:
        await _apply(conn)
    else:
        async with get_connection(db_path) as db:
            await _apply(db)

    # 3. Recarrega state cache com DB agora sincronizado (fonte única de verdade).
    if state is not None:
        await state.reload_from_db(conn=conn, db_path=db_path)

    log.info("position_sync_done", **stats)
    return stats
