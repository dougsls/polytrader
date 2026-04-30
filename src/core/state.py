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
        # ⚠️ ALPHA — Anti-correlação. Mapeia condition_id → set de tokens
        # que o bot detém abertos. Permite detectar conflito (bot tem YES,
        # whale compra NO no mesmo mercado) em O(1) no detect_signal.
        # Sem isso, bot pagaria spread dos dois lados pra ficar neutro.
        self.condition_to_tokens: dict[str, set[str]] = {}

    # --- Bot positions ---------------------------------------------------

    def bot_size(self, token_id: str) -> float:
        return self.bot_positions_by_token.get(token_id, 0.0)

    def bot_set(
        self, token_id: str, size: float, *, condition_id: str | None = None,
    ) -> None:
        """Atualiza posição do bot. Quando `condition_id` é fornecido,
        mantém o mapping `condition_to_tokens` em sync (necessário para
        o anti-correlação gate em `detect_signal`)."""
        if size <= 0:
            self.bot_positions_by_token.pop(token_id, None)
            # Remove o token do set do condition; cleanup se vazio.
            if condition_id is not None:
                tokens = self.condition_to_tokens.get(condition_id)
                if tokens is not None:
                    tokens.discard(token_id)
                    if not tokens:
                        self.condition_to_tokens.pop(condition_id, None)
        else:
            self.bot_positions_by_token[token_id] = size
            if condition_id is not None:
                self.condition_to_tokens.setdefault(condition_id, set()).add(token_id)

    def bot_add(
        self, token_id: str, delta: float, *, condition_id: str | None = None,
    ) -> float:
        """Retorna o novo size. Remove se <= 0.

        `condition_id` opcional para manter `condition_to_tokens` em sync
        (anti-correlação). `apply_fill` passa; outros callers podem omitir.
        """
        new = self.bot_positions_by_token.get(token_id, 0.0) + delta
        self.bot_set(token_id, new, condition_id=condition_id)
        return new

    def has_conflicting_position(self, condition_id: str, token_id: str) -> bool:
        """⚠️ ALPHA — True se o bot já tem posição em OUTRO token do mesmo
        condition_id. Usado pelo `detect_signal` em BUY: se tivermos YES
        aberto e a whale comprar NO, rejeita o sinal pra evitar pagar
        spread dos dois lados (bot ficaria neutro com loss garantida).
        """
        tokens = self.condition_to_tokens.get(condition_id)
        if not tokens:
            return False
        # Conflito = há OUTRO token (≠ token_id passado) com size>0.
        return any(tk != token_id for tk in tokens)

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

    def whale_add(self, wallet: str, token_id: str, delta: float) -> float:
        """Atualiza inventário em tempo real APÓS detectar trade da whale.

        Espelha `bot_add` — usado pelo TradeMonitor inline em
        `_enqueue_trade`. Sem isso, whale_inventory dependeria do snapshot
        do Scanner (a cada 60min) e o Exit Sync calcularia proporção
        baseada em dados defasados.

        BUY da whale → delta positivo (acumula).
        SELL da whale → delta negativo (reduz).

        Resultado clampado em ≥0 via `whale_set` (que pop quando ≤0).
        Retorna o novo size.
        """
        new = self.whale_inventory.get((wallet.lower(), token_id), 0.0) + delta
        self.whale_set(wallet, token_id, new)
        return max(new, 0.0)

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
            self.condition_to_tokens.clear()
            # ⚠️ ALPHA — bootstrap do anti-correlação mapping também.
            # SELECT inclui condition_id pra montar condition_to_tokens.
            async with db.execute(
                "SELECT token_id, condition_id, SUM(size) "
                "FROM bot_positions WHERE is_open=1 "
                "GROUP BY token_id, condition_id"
            ) as cur:
                async for tok, cid, sz in cur:
                    if sz and sz > 0:
                        self.bot_positions_by_token[tok] = float(sz)
                        if cid:
                            self.condition_to_tokens.setdefault(cid, set()).add(tok)
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
