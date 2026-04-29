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
) -> tuple[OrderDraft, CopyTrade, MarketSpec]:
    """Constrói OrderDraft + CopyTrade em RAM. ZERO disk I/O.

    O INSERT em copy_trades é responsabilidade do caller, via
    `asyncio.create_task(persist_trade_async(trade))` DEPOIS do post_order.

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


async def persist_trade_async(
    trade: CopyTrade,
    *,
    conn: aiosqlite.Connection | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    """Fire-and-forget INSERT em copy_trades. Roda em task paralela à post_order.

    Erros aqui NÃO impactam a ordem (que já foi pro CLOB). São
    logados e tracked via metrics; ops podem reconciliar a partir do
    histórico do CLOB se necessário.

    Idempotência: usa INSERT OR REPLACE pra cobrir o caso de duas
    tasks com mesmo trade.id (não acontece hoje, mas defensivo).
    """
    try:
        sql = (
            "INSERT OR REPLACE INTO copy_trades"
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
    except Exception as exc:  # noqa: BLE001 — fire-and-forget loga e segue
        log.error("persist_trade_failed", trade_id=trade.id, err=repr(exc))
