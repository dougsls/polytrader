"""Constrói OrderDraft quantizado + agenda persistência fire-and-forget.

HFT — Hot-Path Hygiene:
    `build_draft` é PURO em RAM (zero I/O de disco). O INSERT de
    copy_trades roda em `persist_trade_async`, despachado via
    `asyncio.create_task` pelo caller APÓS o post_order ter ido pro CLOB.
    Sequência ideal:
        1. build_draft (sub-millisecond, só Decimal + Pydantic)
        2. clob.post_order (~80-130ms RTT NY→London) ◄─── prioridade
        3. asyncio.create_task(persist_trade_async(trade))  fire-and-forget
        4. apply_fill (state cache write-through)
    SQLite WAL é rápido (~1-3ms) mas em batches HFT esses ms acumulam
    direto no signal_to_fill. Tirar do hot path é grátis.

Consome:
    - TradeSignal (preço da baleia, side, token_id, size adjusted via Regra 2)
    - sized_usd aprovado pelo RiskManager
    - ref_price retornado pela Regra 1 (best_ask ou best_bid)
    - MarketSpec (tick_size, neg_risk) do gamma cache

Produz OrderDraft pronto para clob.post_order + CopyTrade em RAM.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiosqlite

from src.api.gamma_client import GammaAPIClient
from src.api.order_builder import MarketSpec, OrderDraft, build_order
from src.core.config import ExecutorConfig
from src.core.database import DEFAULT_DB_PATH, get_connection
from src.core.logger import get_logger
from src.core.models import CopyTrade, TradeSignal

log = get_logger(__name__)

DEFAULT_SIZE_STEP = Decimal("0.01")  # Polymarket padrão


async def _load_market_spec(
    signal: TradeSignal, gamma: GammaAPIClient
) -> MarketSpec:
    market: dict[str, Any] = await gamma.get_market(signal.condition_id)
    tick = market.get("tick_size") or market.get("minimum_tick_size") or 0.01
    neg_risk = bool(market.get("neg_risk"))
    return MarketSpec(
        condition_id=signal.condition_id,
        token_id=signal.token_id,
        tick_size=Decimal(str(tick)),
        size_step=DEFAULT_SIZE_STEP,
        neg_risk=neg_risk,
    )


def _limit_price(ref_price: float, side: str, offset_pct: float) -> float:
    """Preço limite com offset. Clampado em [0.001, 0.999] — outcomes
    Polymarket são probabilidades 0-1; valores fora disso são rejeitados
    pelo CLOB com 400."""
    raw = ref_price * (1 + offset_pct) if side == "BUY" else ref_price * (1 - offset_pct)
    return max(0.001, min(raw, 0.999))


async def build_draft(
    *,
    signal: TradeSignal,
    sized_usd: float,
    ref_price: float,
    gamma: GammaAPIClient,
    cfg: ExecutorConfig,
    db_path: Path = DEFAULT_DB_PATH,
    sell_size_cap_tokens: float | None = None,
) -> tuple[OrderDraft, CopyTrade, MarketSpec]:
    """Constrói OrderDraft + CopyTrade em RAM. ZERO disk I/O.

    Sizing por side (P0 fix):
        BUY  → `raw_size = sized_usd / price` (RiskManager dimensiona em USD).
        SELL → `raw_size = signal.size` (Exit Sync já calculou em tokens
               proporcionais ao % vendido pela whale × bot_size). Usar
               `sized_usd / price` aqui inverteria o sizing inteiro.
        SELL é adicionalmente limitado por `sell_size_cap_tokens` quando
        fornecido — defesa contra vender mais tokens do que o bot detém
        (drift de RAM state vs DB).

    `db_path` permanece na assinatura por compatibilidade — não é usado.
    """
    _ = db_path  # mantido por compat de assinatura; não usado no hot path
    spec = await _load_market_spec(signal, gamma)
    # Em paper_perfect_mirror: sem offset de limit_price (compensação de
    # latência NY→London só faz sentido em live). Em live: aplica offset.
    offset = 0.0 if (cfg.mode != "live" and getattr(cfg, "paper_perfect_mirror", False)) \
                 else cfg.limit_price_offset
    # Realismo paper: fees + slippage simulado degradam o preço efetivo.
    if cfg.mode != "live":
        fees = float(getattr(cfg, "paper_apply_fees", 0.0) or 0.0)
        slip_sim = float(getattr(cfg, "paper_simulate_slippage", 0.0) or 0.0)
        extra = fees + slip_sim
        if extra > 0:
            offset += extra
    raw_price = _limit_price(ref_price, signal.side, offset)
    if signal.side == "SELL":
        raw_size = signal.size
        if sell_size_cap_tokens is not None and sell_size_cap_tokens >= 0:
            raw_size = min(raw_size, sell_size_cap_tokens)
    else:
        raw_size = sized_usd / max(raw_price, 0.01)
    draft = build_order(spec, signal.side, raw_price, raw_size)  # type: ignore[arg-type]

    now = datetime.now(timezone.utc)
    trade = CopyTrade(
        id=str(uuid.uuid4()),
        signal_id=signal.id,
        condition_id=signal.condition_id,
        token_id=signal.token_id,
        side=signal.side,
        intended_size=float(draft.size),
        intended_price=float(draft.price),
        status="pending",
        created_at=now,
    )
    log.info(
        "order_draft_built",
        trade_id=trade.id, side=draft.side,
        price=str(draft.price), size=str(draft.size), neg_risk=draft.neg_risk,
    )
    return draft, trade, spec


async def persist_trade(
    trade: CopyTrade,
    *,
    conn: aiosqlite.Connection | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    """INSERT inicial em copy_trades — chamado SINCRONAMENTE pelo caller
    ANTES do `clob.post_order`. Fix do race INSERT/UPDATE:

    Antes: este INSERT corria em fire-and-forget paralelo a `apply_fill`
    (UPDATE). UPDATE chegava primeiro → 0 rows updated (silently).
    Pior: `INSERT OR REPLACE` sobrescrevia o UPDATE bem-sucedido com
    status='pending', destruindo o audit trail.

    Agora:
      1. `await persist_trade(...)` — garante INSERT antes de qualquer UPDATE.
      2. `INSERT OR IGNORE` — idempotência sem destruir status posterior.
      3. UPDATEs subsequentes (status='submitted'/'filled'/'failed') sempre
         encontram a linha existente.

    Custo: ~1-3ms WAL antes do post_order. Aceitável vs corrupção do trail.
    """
    sql = (
        "INSERT OR IGNORE INTO copy_trades"
        " (id, signal_id, condition_id, token_id, side,"
        "  intended_size, intended_price, executed_size, executed_price,"
        "  slippage, status, created_at, filled_at, error)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    params = (
        trade.id, trade.signal_id, trade.condition_id, trade.token_id,
        trade.side, trade.intended_size, trade.intended_price,
        trade.executed_size, trade.executed_price, trade.slippage,
        trade.status, trade.created_at.isoformat(),
        trade.filled_at.isoformat() if trade.filled_at else None,
        trade.error,
    )
    if conn is not None:
        await conn.execute(sql, params)
        await conn.commit()
    else:
        async with get_connection(db_path) as db:
            await db.execute(sql, params)
            await db.commit()


# Backwards-compat alias — testes/callers antigos importam persist_trade_async.
persist_trade_async = persist_trade


async def update_trade_status(
    trade_id: str,
    *,
    status: str,
    order_id: str | None = None,
    error: str | None = None,
    conn: aiosqlite.Connection | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    """UPDATE atomic do status do trade (submitted/failed/...) sem
    sobrescrever campos de fill. Usado entre o post_order e o
    apply_fill quando o fill é confirmado por user_ws ou matching."""
    fields = ["status=?"]
    values: list[object] = [status]
    if order_id is not None:
        fields.append("order_id=?")
        values.append(order_id)
    if error is not None:
        fields.append("error=?")
        values.append(error[:500])
    values.append(trade_id)
    sql = f"UPDATE copy_trades SET {', '.join(fields)} WHERE id=?"
    if conn is not None:
        await conn.execute(sql, tuple(values))
        await conn.commit()
    else:
        async with get_connection(db_path) as db:
            await db.execute(sql, tuple(values))
            await db.commit()
