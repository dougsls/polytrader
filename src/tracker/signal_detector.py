"""Detecção e qualificação de sinais de copy-trading.

Responsabilidades (nesta ordem):
  1. Construir TradeSignal a partir de um trade bruto (polling ou RTDS).
  2. Aplicar filtro de idade (signal_max_age_seconds).
  3. Aplicar filtro de tamanho mínimo (min_trade_size_usd).
  4. Aplicar **filtro de duração de mercado** (end_date via gamma).
  5. **Regra 2 — Exit Syncing:** para SELL, verificar:
       a. bot possui o token em `bot_positions` (is_open=1). Se não,
          skip com reason="NO_POSITION_TO_SELL".
       b. calcular o tamanho proporcional ao % que a baleia vendeu
          (delta do whale_inventory). O sinal carrega esse tamanho
          ajustado em vez do tamanho cru do trade.
"""
from __future__ import annotations

import time
import uuid  # noqa: F401 — kept for fallback
from datetime import datetime, timezone
from itertools import count
from pathlib import Path
from typing import Any

import aiosqlite

_signal_counter = count(0)

from src.api.data_client import DataAPIClient
from src.api.gamma_client import GammaAPIClient
from src.core.config import TrackerConfig
from src.core.database import DEFAULT_DB_PATH, get_connection
from src.core.logger import get_logger
from src.core.models import TradeSignal

log = get_logger(__name__)


async def _bot_position_size(
    token_id: str,
    *,
    db_path: Path = DEFAULT_DB_PATH,
) -> float:
    async with get_connection(db_path) as db:
        async with db.execute(
            "SELECT COALESCE(SUM(size), 0) FROM bot_positions "
            "WHERE token_id=? AND is_open=1",
            (token_id,),
        ) as cur:
            row = await cur.fetchone()
    return float(row[0]) if row and row[0] else 0.0


def _hours_to_resolution(end_date_iso: str | None) -> float | None:
    if not end_date_iso:
        return None
    end = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
    return (end - datetime.now(timezone.utc)).total_seconds() / 3600.0


async def detect_signal(
    *,
    trade: dict[str, Any],
    wallet_score: float,
    cfg: TrackerConfig,
    gamma: GammaAPIClient,
    data_client: DataAPIClient,  # noqa: ARG001 — reservado para futura validação on-chain
    db_path: Path = DEFAULT_DB_PATH,
    conn: aiosqlite.Connection | None = None,
) -> TradeSignal | None:
    """Retorna TradeSignal qualificado ou None se o trade deve ser ignorado.

    Logs um motivo quando filtra (auditoria).
    """
    now = datetime.now(timezone.utc)

    # --- Idade -----------------------------------------------------------
    ts = trade.get("timestamp") or trade.get("time") or trade.get("t")
    if ts:
        trade_dt = (
            datetime.fromtimestamp(int(ts), tz=timezone.utc)
            if isinstance(ts, (int, float))
            else datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        )
        age = (now - trade_dt).total_seconds()
        if age > cfg.signal_max_age_seconds:
            log.info("signal_stale", age_s=age)
            return None
    else:
        trade_dt = now

    # --- Campos obrigatórios --------------------------------------------
    wallet = trade.get("maker") or trade.get("makerAddress") or trade.get("user")
    condition_id = trade.get("conditionId") or trade.get("condition_id")
    token_id = trade.get("asset") or trade.get("tokenId") or trade.get("token_id")
    side_raw = (trade.get("side") or "").upper()
    if not (wallet and condition_id and token_id and side_raw in ("BUY", "SELL")):
        return None
    size = float(trade.get("size") or trade.get("amount") or 0)
    price = float(trade.get("price") or 0)
    usd_value = size * price if price > 0 else float(trade.get("usdSize") or 0)

    if usd_value < cfg.min_trade_size_usd:
        log.info("signal_too_small", usd=usd_value)
        return None

    # --- Filtro de duração de mercado -----------------------------------
    market = await gamma.get_market(condition_id)
    end_iso = market.get("end_date_iso") or market.get("end_date")
    # Inlined de _hours_to_resolution — usa `now` já capturado acima.
    if end_iso:
        end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        hours = (end_dt - now).total_seconds() / 3600.0
    else:
        hours = None
    f = cfg.market_duration_filter
    if f.enabled:
        if hours is None:
            if f.fallback_behavior == "skip":
                log.info("signal_no_end_date_skipped", condition_id=condition_id)
                return None
        else:
            if hours > f.hard_block_days * 24:
                log.info("signal_hard_block", hours=hours)
                return None
            if hours > f.max_hours_to_resolution:
                log.info("signal_too_long", hours=hours)
                return None
            if hours < f.min_hours_to_resolution:
                log.info("signal_too_close", hours=hours)
                return None

    # --- Regra 2: Exit Syncing para SELL --------------------------------
    adjusted_size = size
    if side_raw == "SELL":
        # Reutiliza conexão injetada pelo tracker em prod (1 conn-open por
        # processo, não por sinal). Fallback para abrir conn só em tests
        # legados que ainda não injetam.
        async def _do_lookups(db: aiosqlite.Connection) -> tuple[float, float]:
            async with db.execute(
                "SELECT COALESCE(SUM(size), 0) FROM bot_positions "
                "WHERE token_id=? AND is_open=1",
                (token_id,),
            ) as cur:
                r1 = await cur.fetchone()
            bot = float(r1[0]) if r1 and r1[0] else 0.0
            if bot <= 0:
                return bot, 0.0
            async with db.execute(
                "SELECT size FROM whale_inventory "
                "WHERE wallet_address=? AND token_id=?",
                (wallet, token_id),
            ) as cur:
                r2 = await cur.fetchone()
            return bot, (float(r2[0]) if r2 else 0.0)

        if conn is not None:
            bot_size, prior_whale = await _do_lookups(conn)
        else:
            async with get_connection(db_path) as db:
                bot_size, prior_whale = await _do_lookups(db)
        if bot_size <= 0:
            log.info("exit_sync_no_position", wallet=wallet, token=token_id)
            return None
        if prior_whale <= 0:
            # Não temos snapshot prévio; conservadoramente use 100% — ou
            # melhor: descarte este SELL e deixe o próximo ciclo capturar.
            log.info("exit_sync_no_whale_snapshot", wallet=wallet, token=token_id)
            return None
        pct_sold = min(size / prior_whale, 1.0)
        adjusted_size = bot_size * pct_sold
        log.info(
            "exit_sync_resize",
            whale_sold_pct=pct_sold, bot_size=bot_size,
            adjusted_size=adjusted_size,
        )

    market_title = market.get("question") or market.get("title") or ""
    outcome = next(
        (t.get("outcome", "") for t in market.get("tokens", []) if t.get("token_id") == token_id),
        "",
    )

    # model_construct pula validação — dados vêm do nosso código, não de input
    # externo. Em prod o TradeSignal sai do signal_detector e entra no executor
    # como objeto interno confiável; não há usuário fornecendo esses campos.
    market_end_date = (
        datetime.fromisoformat(end_iso.replace("Z", "+00:00")) if end_iso else None
    )
    # ID = ns-timestamp + counter (~100× mais rápido que uuid4, ainda único
    # por processo). Persistência no DB usa PRIMARY KEY; não precisa ser RFC4122.
    signal_id = f"{time.time_ns()}-{next(_signal_counter)}"
    return TradeSignal.model_construct(
        id=signal_id,
        wallet_address=wallet,
        wallet_score=wallet_score,
        condition_id=condition_id,
        token_id=token_id,
        side=side_raw,
        size=adjusted_size,
        price=price,
        usd_value=adjusted_size * price if price > 0 else usd_value,
        market_title=market_title,
        outcome=outcome,
        market_end_date=market_end_date,
        hours_to_resolution=hours,
        detected_at=trade_dt,
        source="websocket",
        status="pending",
        skip_reason=None,
    )
