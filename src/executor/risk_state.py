"""Construção de RiskState vivo a partir de balance_cache + DB.

Antes: main.py usava `_default_state()` zerado → risk_manager bloqueava
tudo com PORTFOLIO_CAP. Agora fazemos uma query agregada + leitura do
balance cache para produzir um snapshot real.

Chamado pelo CopyEngine a cada sinal (via `state_provider` lambda) e
periodicamente persistido em `risk_snapshots` para auditoria.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from src.core.database import DEFAULT_DB_PATH, get_connection
from src.core.logger import get_logger
from src.core.models import RiskState
from src.executor.balance_cache import BalanceCache

log = get_logger(__name__)


async def build_risk_state(
    *,
    balance_cache: BalanceCache,
    db_path: Path = DEFAULT_DB_PATH,
    conn: aiosqlite.Connection | None = None,
) -> RiskState:
    """Agrega posição + PnL + portfolio em tempo real.

    - `total_portfolio_value` = saldo USDC + valor de posições abertas
      ao preço corrente (current_price quando disponível, senão
      avg_entry_price).
    - `total_invested` = Σ (size × avg_entry_price) das posições abertas.
    - `daily_pnl` = soma de realized_pnl nas últimas 24h + unrealized.
    """
    async def _query(
        db: aiosqlite.Connection,
    ) -> tuple[float, float, float, int, float, float]:
        async with db.execute(
            "SELECT COUNT(*), "
            "       COALESCE(SUM(size * avg_entry_price), 0), "
            "       COALESCE(SUM(size * COALESCE(current_price, avg_entry_price)), 0), "
            "       COALESCE(SUM(unrealized_pnl), 0), "
            "       COALESCE(SUM(realized_pnl), 0) "
            "FROM bot_positions WHERE is_open=1"
        ) as cur:
            row = await cur.fetchone()
        open_count = int(row[0]) if row else 0
        invested = float(row[1]) if row else 0.0
        current_value = float(row[2]) if row else 0.0
        unrealized = float(row[3]) if row else 0.0
        realized_lifetime = float(row[4]) if row else 0.0

        day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        async with db.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM bot_positions "
            "WHERE closed_at >= ?",
            (day_ago,),
        ) as cur:
            row2 = await cur.fetchone()
        daily_realized = float(row2[0]) if row2 else 0.0
        # Retorna realized_lifetime SEPARADO de daily_realized — antes
        # ambos eram somados como `realized_total` e o daily_pnl recebia
        # só `unrealized`, fazendo o circuit breaker max_daily_loss
        # nunca disparar.
        return (
            invested, current_value, unrealized, open_count,
            realized_lifetime, daily_realized,
        )

    if conn is not None:
        invested, current_value, unrealized, open_count, realized_lifetime, daily_realized = (
            await _query(conn)
        )
    else:
        async with get_connection(db_path) as db:
            invested, current_value, unrealized, open_count, realized_lifetime, daily_realized = (
                await _query(db)
            )

    usdc = balance_cache.balance_usdc if balance_cache.is_fresh else 0.0
    portfolio_value = usdc + current_value

    # ⚠️ DRAWDOWN CORRETO — peak-to-trough, não loss instantâneo.
    # Antes: (invested - current) / invested calculava só loss vs entrada.
    # Agora: (peak - current) / peak. Peak é mantido pelo BalanceCache
    # (RAM persistido + bootstrap via risk_snapshots no startup).
    peak = balance_cache.note_portfolio_value(portfolio_value)
    current_dd = 0.0
    if peak > 0:
        loss_from_peak = max(peak - portfolio_value, 0.0)
        current_dd = loss_from_peak / peak

    # ⚠️ DAILY PNL CORRETO — realized 24h + unrealized atual.
    # Antes: daily_pnl = unrealized (ignorava perdas realizadas das 24h).
    # Resultado: max_daily_loss_usd nunca disparava em dia ruim de SELLs.
    daily_pnl = daily_realized + unrealized

    return RiskState(
        total_portfolio_value=portfolio_value,
        total_invested=invested,
        total_unrealized_pnl=unrealized,
        total_realized_pnl=realized_lifetime,
        daily_pnl=daily_pnl,
        max_drawdown=current_dd,
        current_drawdown=current_dd,
        open_positions=open_count,
    )
