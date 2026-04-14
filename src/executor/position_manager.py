"""UPSERT de bot_positions — fonte da verdade do inventário do bot.

Regras:
    BUY  → se não há posição aberta, cria; senão, soma size e recalcula
           avg_entry_price ponderado.
    SELL → decrementa size; se size ≤ ε, fecha (is_open=0, closed_at=now).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import orjson

from src.core.database import DEFAULT_DB_PATH, get_connection
from src.core.logger import get_logger
from src.core.models import CopyTrade, TradeSignal
from src.core.state import InMemoryState

log = get_logger(__name__)

_EPS = 1e-9


async def apply_fill(
    *,
    signal: TradeSignal,
    trade: CopyTrade,
    executed_size: float,
    executed_price: float,
    db_path: Path = DEFAULT_DB_PATH,
    state: InMemoryState | None = None,
    conn: aiosqlite.Connection | None = None,
) -> None:
    """Atualiza bot_positions a partir de um fill confirmado.

    Se `state` for fornecido, faz write-through no cache RAM — mantém a
    consistência com o Exit Syncing do detect_signal (Fase 4).
    Se `conn` for fornecido, reutiliza a conexão compartilhada (economiza
    ~200μs de thread-init por fill).
    """
    if state is not None:
        delta = executed_size if signal.side == "BUY" else -executed_size
        state.bot_add(signal.token_id, delta)
    now = datetime.now(timezone.utc).isoformat()

    async def _do_writes(db: aiosqlite.Connection) -> None:
        async with db.execute(
            "SELECT id, size, avg_entry_price, source_wallets_json "
            "FROM bot_positions WHERE token_id=? AND is_open=1",
            (signal.token_id,),
        ) as cur:
            row = await cur.fetchone()

        if signal.side == "BUY":
            if row is None:
                await db.execute(
                    """
                    INSERT INTO bot_positions
                        (condition_id, token_id, market_title, outcome, size,
                         avg_entry_price, current_price, source_wallets_json,
                         opened_at, is_open, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        signal.condition_id, signal.token_id, signal.market_title,
                        signal.outcome, executed_size, executed_price, executed_price,
                        orjson.dumps([signal.wallet_address]).decode(),
                        now, now,
                    ),
                )
            else:
                old_size = float(row[1])
                old_avg = float(row[2])
                new_size = old_size + executed_size
                new_avg = (
                    (old_size * old_avg + executed_size * executed_price) / new_size
                    if new_size > 0 else executed_price
                )
                wallets = orjson.loads(row[3])
                if signal.wallet_address not in wallets:
                    wallets.append(signal.wallet_address)
                await db.execute(
                    "UPDATE bot_positions SET size=?, avg_entry_price=?, "
                    "current_price=?, source_wallets_json=?, updated_at=? WHERE id=?",
                    (new_size, new_avg, executed_price,
                     orjson.dumps(wallets).decode(), now, row[0]),
                )
        else:  # SELL
            if row is None:
                log.warning("sell_without_position", token_id=signal.token_id)
                return
            old_size = float(row[1])
            new_size = old_size - executed_size
            if new_size <= _EPS:
                await db.execute(
                    "UPDATE bot_positions SET size=0, is_open=0, "
                    "closed_at=?, updated_at=?, current_price=? WHERE id=?",
                    (now, now, executed_price, row[0]),
                )
            else:
                await db.execute(
                    "UPDATE bot_positions SET size=?, current_price=?, updated_at=? "
                    "WHERE id=?",
                    (new_size, executed_price, now, row[0]),
                )

        await db.execute(
            "UPDATE copy_trades SET executed_size=?, executed_price=?, "
            "slippage=?, status='filled', filled_at=? WHERE id=?",
            (
                executed_size, executed_price,
                (executed_price / signal.price - 1.0) if signal.price > 0 else None,
                now, trade.id,
            ),
        )
        await db.commit()

    if conn is not None:
        await _do_writes(conn)
    else:
        async with get_connection(db_path) as db:
            await _do_writes(db)
