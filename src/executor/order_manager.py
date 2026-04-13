"""Constrói OrderDraft quantizado + persiste CopyTrade.

Consome:
    - TradeSignal (preço da baleia, side, token_id, size adjusted via Regra 2)
    - sized_usd aprovado pelo RiskManager
    - ref_price retornado pela Regra 1 (best_ask ou best_bid)
    - MarketSpec (tick_size, neg_risk) do gamma cache

Produz OrderDraft pronto para clob.post_order.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

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
    """Preço limite com offset compensando RTT NY→London ~100ms."""
    if side == "BUY":
        return ref_price * (1 + offset_pct)
    return ref_price * (1 - offset_pct)


async def build_draft(
    *,
    signal: TradeSignal,
    sized_usd: float,
    ref_price: float,
    gamma: GammaAPIClient,
    cfg: ExecutorConfig,
    db_path: Path = DEFAULT_DB_PATH,
) -> tuple[OrderDraft, CopyTrade, MarketSpec]:
    spec = await _load_market_spec(signal, gamma)
    raw_price = _limit_price(ref_price, signal.side, cfg.limit_price_offset)
    # size em tokens = USD alocado / preço
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

    async with get_connection(db_path) as db:
        await db.execute(
            """
            INSERT INTO copy_trades
                (id, signal_id, condition_id, token_id, side,
                 intended_size, intended_price, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                trade.id, trade.signal_id, trade.condition_id, trade.token_id,
                trade.side, trade.intended_size, trade.intended_price,
                now.isoformat(),
            ),
        )
        await db.commit()

    log.info(
        "order_draft_built",
        trade_id=trade.id, side=draft.side,
        price=str(draft.price), size=str(draft.size), neg_risk=draft.neg_risk,
    )
    return draft, trade, spec
