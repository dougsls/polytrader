"""In-memory state cache — Exit Syncing em RAM (Fase 4).

Mantém duas visões materializadas que o DB populou, atualizadas por write-
through nos módulos de produção:

    * `bot_positions_by_token[token_id] = size`
        — usado por `detect_signal` para decidir se bot tem posição aberta.
          Hit cache → zero SQLite lookup no hot path.
    * `whale_inventory[(wallet, token_id)] = size`
        — usado para calcular % proporcional no SELL (Regra 2).

Invariantes:
    * Escritas são feitas SEMPRE via `position_manager.apply_fill` (bot)
      e `whale_inventory.snapshot_whale` (baleia). Se DB e RAM divergirem,
      DB é fonte de verdade → `reload_from_db()` sincroniza.
    * Thread-safe: toda mutação passa pelo event loop principal (single
      writer); leituras são atômicas no dict (GIL).
"""
from __future__ import annotations

from pathlib import Path

import aiosqlite

from src.core.database import DEFAULT_DB_PATH
from src.core.logger import get_logger

log = get_logger(__name__)


class InMemoryState:
    """Snapshot mutável compartilhado pelo tracker + executor."""

    def __init__(self) -> None:
        self.bot_positions_by_token: dict[str, float] = {}
        self.whale_inventory: dict[tuple[str, str], float] = {}

    # --- Bot positions ---------------------------------------------------

    def bot_size(self, token_id: str) -> float:
        return self.bot_positions_by_token.get(token_id, 0.0)

    def bot_set(self, token_id: str, size: float) -> None:
        if size <= 0:
            self.bot_positions_by_token.pop(token_id, None)
        else:
            self.bot_positions_by_token[token_id] = size

    def bot_add(self, token_id: str, delta: float) -> float:
        """Retorna o novo size. Remove se <= 0."""
        new = self.bot_positions_by_token.get(token_id, 0.0) + delta
        self.bot_set(token_id, new)
        return new

    # --- Whale inventory -------------------------------------------------
    # wallet é sempre armazenado lowercase. Os callers podem passar
    # qualquer case — normalizamos aqui, evitando duplicatas em dict.

    def whale_size(self, wallet: str, token_id: str) -> float:
        return self.whale_inventory.get((wallet.lower(), token_id), 0.0)

    def whale_set(self, wallet: str, token_id: str, size: float) -> None:
        key = (wallet.lower(), token_id)
        if size <= 0:
            self.whale_inventory.pop(key, None)
        else:
            self.whale_inventory[key] = size

    # --- Sync ------------------------------------------------------------

    async def reload_from_db(
        self,
        conn: aiosqlite.Connection | None = None,
        *,
        db_path: Path = DEFAULT_DB_PATH,
    ) -> None:
        """Warm-up no startup ou após crash. Lê snapshot completo do DB."""
        from src.core.database import get_connection

        async def _load(db: aiosqlite.Connection) -> None:
            self.bot_positions_by_token.clear()
            self.whale_inventory.clear()
            async with db.execute(
                "SELECT token_id, SUM(size) FROM bot_positions "
                "WHERE is_open=1 GROUP BY token_id"
            ) as cur:
                async for tok, sz in cur:
                    if sz and sz > 0:
                        self.bot_positions_by_token[tok] = float(sz)
            async with db.execute(
                "SELECT wallet_address, token_id, size FROM whale_inventory"
            ) as cur:
                async for w, tok, sz in cur:
                    if sz and sz > 0:
                        self.whale_inventory[(w.lower(), tok)] = float(sz)

        if conn is not None:
            await _load(conn)
        else:
            async with get_connection(db_path) as db:
                await _load(db)

        log.info(
            "state_reloaded",
            bot_tokens=len(self.bot_positions_by_token),
            whale_entries=len(self.whale_inventory),
        )
