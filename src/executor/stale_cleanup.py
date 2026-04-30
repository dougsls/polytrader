"""H4 — Stale position cleanup (stop-loss em tempo).

A Regra 2 (Exit Syncing) só dispara SELL quando a baleia vende. Se a
baleia esquece a posição (ou sai sem nós detectarmos), capital fica
preso até resolução do mercado. Esta task percorre `bot_positions`
abertas e emite um `TradeSignal` SELL sintético quando a posição
ultrapassa `max_position_age_hours`.

⚠️ DEATH-TRAP FIX:
    O sinal sintético usa `current_price` (do price_updater) como
    anchor — NÃO o `avg_entry_price`. Adicionalmente seta
    `bypass_slippage_check=True` para o copy_engine pular a Regra 1
    (Anti-Slippage Anchoring). Sem esses dois fixes, uma posição
    comprada a 0.50¢ que despencou para 0.05¢ ficaria presa pra
    sempre — Regra 1 abortaria com slippage 90%.

    Isto é um stop-loss puro de tempo. A intenção é liquidar a
    qualquer custo após max_position_age_hours. Spread shield ainda
    valida book não-zerado.

Roda a cada 5 minutos, só em mode=live|paper.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import aiosqlite

from src.core.config import ExecutorConfig
from src.core.logger import get_logger
from src.core.models import TradeSignal

log = get_logger(__name__)

CHECK_INTERVAL_SECONDS = 5 * 60


async def _find_stale(
    conn: aiosqlite.Connection, max_age_hours: float,
) -> list[tuple[str, str, str, str, float, float, float | None]]:
    """Retorna (cid, token_id, title, outcome, size, avg_price, current_price).

    `current_price` vem do price_updater (atualizado a cada 30s);
    pode ser None nos primeiros segundos pós-startup.
    """
    cutoff = datetime.now(timezone.utc).timestamp() - (max_age_hours * 3600)
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    async with conn.execute(
        "SELECT condition_id, token_id, market_title, outcome, "
        "       size, avg_entry_price, current_price "
        "FROM bot_positions WHERE is_open=1 AND opened_at < ?",
        (cutoff_iso,),
    ) as cur:
        rows = await cur.fetchall()
    return [
        (r[0], r[1], r[2], r[3], float(r[4]), float(r[5]),
         float(r[6]) if r[6] is not None else None)
        for r in rows
    ]


def _synthetic_sell_signal(
    condition_id: str, token_id: str, title: str, outcome: str,
    size: float, avg_price: float, current_price: float | None,
) -> TradeSignal:
    """SELL sintético — stop-loss em tempo. Usa current_price como
    anchor (NÃO avg_price) + bypass slippage check.

    ⚠️ DEATH-TRAP FIX:
        - anchor = current_price (preço de mercado real). Em ausência
          (price_updater ainda não rodou), fallback ao avg_price; o
          bypass garante que não trava mesmo assim.
        - bypass_slippage_check=True instrui copy_engine a pular
          check_slippage_or_abort. Caso contrário, posição despencada
          ficaria presa pra sempre.
    """
    # Use current_price quando disponível e plausível; senão avg_price
    # com bypass garante que copy_engine não trava no slippage check.
    anchor_price = (
        current_price
        if current_price is not None and current_price > 0
        else avg_price
    )
    return TradeSignal.model_construct(
        id=f"stale-{uuid.uuid4().hex[:12]}",
        wallet_address="__stale_cleanup__",
        wallet_score=1.0,  # Bypass min_confidence_score
        condition_id=condition_id, token_id=token_id,
        side="SELL", size=size, price=anchor_price,
        usd_value=size * anchor_price,
        market_title=title, outcome=outcome,
        market_end_date=None, hours_to_resolution=None,
        detected_at=datetime.now(timezone.utc),
        source="polling", status="pending", skip_reason=None,
        # CRÍTICO: bypass slippage anchor (Regra 1) — força dump
        # mesmo que current_price << avg_price.
        bypass_slippage_check=True,
    )


async def stale_position_cleanup_loop(
    *,
    shutdown: asyncio.Event,
    cfg: ExecutorConfig,
    conn: aiosqlite.Connection,
    signal_queue: asyncio.Queue[TradeSignal],
) -> None:
    if cfg.mode == "dry-run":
        return
    while not shutdown.is_set():
        try:
            await asyncio.wait_for(
                shutdown.wait(), timeout=CHECK_INTERVAL_SECONDS,
            )
            return
        except asyncio.TimeoutError:
            pass
        try:
            stale = await _find_stale(conn, cfg.max_position_age_hours)
            for cid, tok, title, outcome, size, avg_price, cur_price in stale:
                sig = _synthetic_sell_signal(
                    cid, tok, title, outcome, size, avg_price, cur_price,
                )
                try:
                    signal_queue.put_nowait(sig)
                    log.warning(
                        "stale_position_auto_sell",
                        token_id=tok, size=size,
                        age_limit_h=cfg.max_position_age_hours,
                        anchor=sig.price, avg_entry=avg_price,
                        bypass_slippage=True,
                    )
                except asyncio.QueueFull:
                    log.warning("stale_cleanup_queue_full", token_id=tok)
        except Exception as exc:  # noqa: BLE001
            log.error("stale_cleanup_failed", err=repr(exc))
