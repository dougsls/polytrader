"""Detecção e qualificação de sinais de copy-trading.

Responsabilidades (nesta ordem):
  1. Parse UNA VEZ via `parse_trade_event` (HFT — sub-µs slot access).
  2. Aplicar filtro de idade (signal_max_age_seconds).
  3. Aplicar filtro de tamanho mínimo (min_trade_size_usd).
  4. Aplicar **filtro de duração de mercado** (end_date via gamma).
  5. **Regra 2 — Exit Syncing:** para SELL, verificar:
       a. bot possui o token em `bot_positions` (is_open=1). Se não,
          skip com reason="NO_POSITION_TO_SELL".
       b. calcular tamanho proporcional. Quando o payload contém
          `currentSize` (saldo da whale pós-trade), usa isso pra
          calcular pct_sold real — imune a drift do RAM cache. Se
          whale zerou (current=0): close TUDO da nossa posição.
       c. Drift correction: state.whale_set(current) atualiza nossa
          visão do inventário da whale com o ground-truth do payload.
"""
from __future__ import annotations

import time
import uuid  # noqa: F401 — kept for fallback
from datetime import datetime, timezone
from itertools import count
from pathlib import Path
from typing import Any

import aiosqlite

from src.api.data_client import DataAPIClient
from src.api.gamma_client import GammaAPIClient
from src.core.config import TrackerConfig
from src.core.database import DEFAULT_DB_PATH, get_connection
from src.core.logger import get_logger
from src.core.models import TradeSignal
from src.core.state import InMemoryState
from src.core.trade_event import TradeEvent, parse_trade_event

_signal_counter = count(0)
log = get_logger(__name__)


def _compute_exit_size(
    *,
    size: float,
    prior_whale: float,
    bot_size: float,
    current_whale: float | None,
) -> tuple[float, float, str]:
    """Calcula (adjusted_size, pct_sold, source) para Exit Syncing.

    Função pura sem I/O — testável isoladamente.

    Prioridade de fontes (HFT — drift correction):
      1. `current_whale == 0` → whale zerou; close TUDO (`bot_size`)
      2. `current_whale > 0` → exato: pct = (prior - current) / prior
      3. `current_whale is None` → fallback legado: pct = size/prior

    Returns:
        (adjusted_size, pct_sold, source) onde source ∈ {"event_close",
        "event_delta", "size_proxy"} para auditoria nos logs.
    """
    if current_whale is not None and current_whale <= 0.0:
        # Whale zerou — full exit. Override matemático do `size` cru
        # (que pode ser parcial e perdemos eventos anteriores).
        return bot_size, 1.0, "event_close"

    if current_whale is not None and current_whale > 0.0 and prior_whale > 0.0:
        # Delta REAL do evento. Imune a drift no `prior_whale`.
        # max(0) defende contra current > prior (whale comprou e nosso
        # prior_whale ficou pequeno demais por missed packet).
        delta = max(0.0, prior_whale - current_whale)
        pct = min(delta / prior_whale, 1.0)
        return bot_size * pct, pct, "event_delta"

    # Fallback legado — sem ground truth do payload.
    pct = min(size / prior_whale, 1.0)
    return bot_size * pct, pct, "size_proxy"


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
    data_client: DataAPIClient,  # noqa: ARG001
    db_path: Path = DEFAULT_DB_PATH,
    conn: aiosqlite.Connection | None = None,
    state: InMemoryState | None = None,
    whale_portfolio_usd: float | None = None,
    whale_win_rate: float | None = None,
    parsed: TradeEvent | None = None,
    confluence_count: int = 1,
) -> TradeSignal | None:
    """Retorna TradeSignal qualificado ou None se o trade deve ser ignorado.

    Logs um motivo quando filtra (auditoria).

    HFT — parser opcional: caller pode passar `parsed` já pronto pra
    economizar uma re-parse. Se None, fazemos uma única passada via
    `parse_trade_event` (slots dataclass, sub-µs).
    """
    now = datetime.now(timezone.utc)

    # HFT — parse UNA VEZ via dataclass com slots (~30% mais rápido que
    # cascata de `.get()`. zero alocação extra de dict).
    evt = parsed if parsed is not None else parse_trade_event(trade)
    if evt is None:
        return None

    # --- Idade — reutiliza evt.timestamp (sem refazer .get) ------------
    if evt.timestamp is not None:
        ts = evt.timestamp
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

    # Campos críticos já validados pelo parser — extrai pra locals
    # (slot lookup mais barato que repetir evt.field N vezes em hot path).
    wallet = evt.wallet
    condition_id = evt.condition_id
    token_id = evt.token_id
    side_raw = evt.side
    size = evt.size
    price = evt.price
    usd_value = evt.usd_value

    if usd_value < cfg.min_trade_size_usd:
        log.info("signal_too_small", usd=usd_value)
        return None

    # --- ⚠️ ALPHA — Anti-correlação (BUY only) --------------------------
    # Se o bot já tem posição em OUTRO token do mesmo condition_id e a
    # whale está comprando o token oposto, rejeita: pagar spread em
    # ambos os lados nos faz neutro com fee loss garantido.
    # SELL não é bloqueado — é exit, não entrada nova.
    if side_raw == "BUY" and state is not None:
        if state.has_conflicting_position(condition_id, token_id):
            log.info(
                "signal_skipped_conflicting_position",
                wallet=wallet, condition_id=condition_id,
                incoming_token=token_id,
            )
            return None

    # --- Filtro de duração de mercado -----------------------------------
    market = await gamma.get_market(condition_id)
    end_iso = (
        market.get("end_date_iso")
        or market.get("end_date")
        or market.get("endDate")  # Gamma 2026: camelCase
    )
    # Inlined de _hours_to_resolution — usa `now` já capturado acima.
    # Polymarket rotaciona formatos (Z / +00:00 / naive). Normaliza tz.
    if end_iso:
        end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        hours = (end_dt - now).total_seconds() / 3600.0
    else:
        end_dt = None
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
    # HFT — Drift Correction (asymmetry fix):
    # Quando o payload trade contém `currentSize` (saldo da whale APÓS
    # o trade), o cálculo de pct_sold é matematicamente exato — imune
    # a drift do `prior_whale` em RAM (que pode estar defasado por
    # missed packet RTDS, AMM split/merge, ou crash do bot anterior).
    #
    #   pct_sold_exact = (prior_whale - current_whale) / prior_whale
    #
    # Caso especial: current_whale == 0 → whale ZEROU. Nossa posição
    # também deve ir a zero (`adjusted_size = bot_size`), independente
    # do `size` reportado no evento (que pode ser parcial vs evento
    # final consolidado de close-all).
    #
    # Fallback (current_whale ausente): comportamento legado
    # `pct_sold = min(size / prior_whale, 1.0)` — vulnerável a drift
    # mas era o único disponível antes.
    adjusted_size = size
    if side_raw == "SELL":
        # Fase 4 — Fast path: in-memory state cache (< 1μs vs ~500μs SQLite).
        # Produção sempre injeta `state`; fallback cobre tests legados.
        if state is not None:
            bot_size = state.bot_size(token_id)
            if bot_size <= 0:
                log.info("exit_sync_no_position", wallet=wallet, token=token_id)
                return None
            prior_whale = state.whale_size(wallet, token_id)
            if prior_whale <= 0:
                log.info("exit_sync_no_whale_snapshot", wallet=wallet, token=token_id)
                return None
            adjusted_size, pct_sold, source = _compute_exit_size(
                size=size, prior_whale=prior_whale, bot_size=bot_size,
                current_whale=evt.current_whale_size,
            )
            # Drift correction — atualiza state com ground truth do evento.
            # Próximo SELL desta whale neste token usa o saldo REAL.
            if evt.current_whale_size is not None:
                state.whale_set(wallet, token_id, evt.current_whale_size)
            log.info(
                "exit_sync_resize",
                whale_sold_pct=pct_sold, bot_size=bot_size,
                adjusted_size=adjusted_size, source=source,
                current_whale=evt.current_whale_size,
            )
        else:
            # Fallback DB — usado por tests que ainda não injetam state.
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
                log.info("exit_sync_no_whale_snapshot", wallet=wallet, token=token_id)
                return None
            adjusted_size, pct_sold, source = _compute_exit_size(
                size=size, prior_whale=prior_whale, bot_size=bot_size,
                current_whale=evt.current_whale_size,
            )
            log.info(
                "exit_sync_resize",
                whale_sold_pct=pct_sold, bot_size=bot_size,
                adjusted_size=adjusted_size, source=f"db+{source}",
                current_whale=evt.current_whale_size,
            )

    market_title = market.get("question") or market.get("title") or ""
    outcome = next(
        (t.get("outcome", "") for t in market.get("tokens", []) if t.get("token_id") == token_id),
        "",
    )

    # RISK MGMT — Anti-fragmentação. Quando o RTDS emite `currentSize`,
    # carregamos no signal o INVENTÁRIO TOTAL da whale neste token APÓS
    # o fill (em USD). Em CLOBs, ordens grandes (~$100k) chegam como
    # múltiplos fills (~$10k cada). Sem este campo, whale_proportional
    # calcularia convicção 10× pequena e bot compraria picado.
    # Para BUY: current_whale_size é o saldo que cresceu com o trade.
    # Para SELL: representa o residual; sizing de SELL não usa este
    # caminho (Exit Sync sobrescreve via _compute_exit_size).
    if evt.current_whale_size is not None and evt.current_whale_size > 0 and price > 0:
        whale_total_position_usd: float | None = evt.current_whale_size * price
    else:
        whale_total_position_usd = None

    # model_construct pula validação — dados vêm do nosso código, não de input
    # externo. Reutiliza end_dt já parseado + normalizado acima (sem 2º parse).
    market_end_date = end_dt
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
        whale_portfolio_usd=whale_portfolio_usd,
        whale_win_rate=whale_win_rate,
        whale_total_position_usd=whale_total_position_usd,
        confluence_count=confluence_count,
    )
