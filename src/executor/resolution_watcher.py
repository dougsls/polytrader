"""Detecta resolução de mercados, fecha bot_positions abertas E redime
USDC on-chain via CTF.redeemPositions (FUND-LOCK FIX).

⚠️ FUND-LOCK CRÍTICO:
    Na Polymarket (CTF), quando um mercado resolve, os tokens vencedores
    valem 1 USDC face value mas NÃO transferem automaticamente. É preciso
    chamar ConditionalTokens.redeemPositions(USDC, parentId, conditionId,
    indexSets) on-chain para queimar os tokens e receber o USDC real.
    Sem essa chamada, o capital fica preso no contrato CTF para sempre.

Cada posição vencedora: tx_hash gravado em bot_positions.redeem_tx_hash.
Idempotência via sentinel "dispatching" — não dispara duas tasks pra
mesma position se dois ticks rodarem em sequência.

⚠️ RATE-LIMIT FIX:
    Antes: 1 request /markets?condition_ids=X (force_refresh) por position
    aberta = 50 reqs/min em rajada → ban Cloudflare.
    Agora: 1 request batch /markets?condition_ids=ID1,ID2,...,IDn (até 50
    por chunk) por tick. Throttled adicionalmente pelo BackgroundRateLimiter.

Fluxo:
  1. SELECT bot_positions abertas → group por condition_id
  2. UMA chamada batch gamma.get_markets_batch(unique_cids)
  3. Para cada position: resolve via market do dict OU local fallback
  4. UPDATE is_open=0, closed_at, realized_pnl, current_price
  5. Para wins: dispatch async ctf.redeem(cid, [indexSet]) → grava tx_hash
"""
from __future__ import annotations

import asyncio
import json

import aiosqlite

from src.api.background_limiter import get_background_limiter
from src.api.gamma_client import GammaAPIClient
from src.arbitrage.ctf_client import CTFClient
from src.core.logger import get_logger

log = get_logger(__name__)

RESOLUTION_CHECK_INTERVAL_SECONDS = 60  # 1min — captura inferência por preço rapidamente


def _parse_outcome_prices(market: dict) -> list[float] | None:
    """outcomePrices vem como string JSON ou lista. Normaliza."""
    raw = market.get("outcomePrices") or market.get("outcome_prices")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if isinstance(raw, list):
        try:
            return [float(p) for p in raw]
        except (TypeError, ValueError):
            return None
    return None


def _outcome_index(market: dict, outcome_label: str) -> int | None:
    """Retorna o índice (0/1) do nosso `outcome` na lista do market."""
    outcomes = market.get("outcomes")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except json.JSONDecodeError:
            return None
    if not isinstance(outcomes, list):
        return None
    try:
        return next(
            i for i, o in enumerate(outcomes)
            if str(o).lower() == str(outcome_label).lower()
        )
    except StopIteration:
        return None


def _outcome_index_set(outcome_idx: int) -> int:
    """CTF binary indexSet bitmask: outcome 0 → 1, outcome 1 → 2.

    redeemPositions consome um array de indexSets; pra um único outcome
    é [1<<idx]. Outcomes complementares: indexSet 1 = outcome[0],
    indexSet 2 = outcome[1].
    """
    return 1 << outcome_idx


async def _local_resolve_if_extreme(
    conn: aiosqlite.Connection, position_id: int, condition_id: str,
    outcome: str, size: float, avg_entry: float,
) -> tuple[str, float] | None:
    """Fallback: usa DB local (current_price do price_updater) pra inferir
    resolução. Polymarket CLOB para de negociar quando outcome é decidido —
    preço em extremo (≥0.99 ou ≤0.01) é indicador confiável de resolução.

    Returns:
        (status, resolution_price) ou None. status ∈ {"won", "lost"}.
    """
    async with conn.execute(
        "SELECT current_price FROM bot_positions WHERE id=?", (position_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    cur_price = float(row[0]) if row[0] else 0.0
    if not (cur_price >= 0.99 or cur_price <= 0.01):
        return None
    resolution_price = 1.0 if cur_price >= 0.99 else 0.0
    realized_pnl = (resolution_price - avg_entry) * size
    won = resolution_price > avg_entry
    now = "datetime('now')"
    await conn.execute(
        f"UPDATE bot_positions SET is_open=0, closed_at={now}, "
        "updated_at=" + now + ", current_price=?, realized_pnl=?, "
        "unrealized_pnl=0, close_reason='resolved' WHERE id=?",
        (resolution_price, realized_pnl, position_id),
    )
    log.info("position_resolved_local_fallback",
             cid=condition_id[:12], outcome=outcome, cur_price=cur_price,
             resolution_price=resolution_price,
             realized_pnl=realized_pnl, won=won)
    return ("won" if won else "lost", resolution_price)


async def _resolve_via_market(
    conn: aiosqlite.Connection,
    position_id: int,
    condition_id: str,
    outcome: str,
    size: float,
    avg_entry: float,
    market: dict | None,
) -> tuple[str, float] | None:
    """Decide o destino de UMA position dado o market dict (do batch).

    Se market is None ou closed=False → tenta local fallback.

    Returns: (status, resolution_price) ou ("still_open", 0.0).
    """
    if market is None:
        # Gamma 404 = mercado resolveu e saiu do filtro active=true.
        # Tenta local extreme.
        result = await _local_resolve_if_extreme(
            conn, position_id, condition_id, outcome, size, avg_entry,
        )
        return result if result else ("still_open", 0.0)

    closed = bool(market.get("closed")) or bool(market.get("resolved"))
    if not closed:
        local = await _local_resolve_if_extreme(
            conn, position_id, condition_id, outcome, size, avg_entry,
        )
        return local if local else ("still_open", 0.0)

    prices = _parse_outcome_prices(market)
    idx = _outcome_index(market, outcome)
    if not prices or idx is None or idx >= len(prices):
        log.warning("resolution_unable_to_parse",
                    cid=condition_id[:12], prices=prices, outcome=outcome)
        return None

    resolution_price = float(prices[idx])
    realized_pnl = (resolution_price - avg_entry) * size
    won = resolution_price > avg_entry

    now = "datetime('now')"
    await conn.execute(
        f"UPDATE bot_positions SET is_open=0, closed_at={now}, "
        "updated_at=" + now + ", current_price=?, realized_pnl=?, "
        "unrealized_pnl=0, close_reason='resolved' WHERE id=?",
        (resolution_price, realized_pnl, position_id),
    )
    log.info(
        "position_resolved",
        cid=condition_id[:12], outcome=outcome,
        resolution_price=resolution_price, entry=avg_entry,
        size=size, realized_pnl=realized_pnl, won=won,
    )
    return ("won" if won else "lost", resolution_price)


async def _dispatch_redeem(
    conn: aiosqlite.Connection,
    ctf: CTFClient | None,
    position_id: int,
    condition_id: str,
    outcome_label: str,
    market: dict | None,
    notifier: object | None = None,
) -> None:
    """Tarefa fire-and-forget que chama CTF.redeemPositions.

    ⚠️ FUND-LOCK FIX. Idempotência: caller já fez UPDATE com sentinel
    redeem_tx_hash="dispatching" antes de spawnar a task. Aqui dentro,
    em sucesso seta o tx_hash real; em falha seta "failed:<msg>" pra
    permitir retry no próximo tick.
    """
    if ctf is None:
        # Mode != live ou sem chave — pula sem desperdício.
        await conn.execute(
            "UPDATE bot_positions SET redeem_tx_hash='paper-skip' WHERE id=?",
            (position_id,),
        )
        await conn.commit()
        return

    # Determinar indexSet via market (preferido) ou inferir do outcome label.
    index_set: int
    if market is not None:
        idx = _outcome_index(market, outcome_label)
        if idx is None:
            # Sem outcomes no market → assume primeiro slot (Yes).
            idx = 0
        index_set = _outcome_index_set(idx)
    else:
        # Fallback: assume convenção Yes=0, No=1 (não-neg-risk binary).
        index_set = 1 if str(outcome_label).lower().startswith("y") else 2

    try:
        tx_hash = await ctf.redeem(condition_id, [index_set])
    except Exception as exc:  # noqa: BLE001
        msg = repr(exc)[:200]
        log.error(
            "redeem_dispatch_failed", cid=condition_id[:12],
            position_id=position_id, err=msg,
        )
        await conn.execute(
            "UPDATE bot_positions SET redeem_tx_hash=? WHERE id=?",
            (f"failed:{msg[:100]}", position_id),
        )
        await conn.commit()
        if notifier is not None:
            try:
                notifier.notify(  # type: ignore[attr-defined]
                    f"🚨 Redeem on-chain falhou pra {condition_id[:12]}…: {msg[:120]}"
                )
            except Exception:  # noqa: BLE001
                pass
        return

    await conn.execute(
        "UPDATE bot_positions SET redeem_tx_hash=? WHERE id=?",
        (tx_hash, position_id),
    )
    await conn.commit()
    log.info(
        "redeem_confirmed", cid=condition_id[:12],
        position_id=position_id, tx=tx_hash, index_set=index_set,
    )


async def check_resolutions_once(
    conn: aiosqlite.Connection,
    gamma: GammaAPIClient,
    ctf: CTFClient | None = None,
    notifier: object | None = None,
) -> dict[str, int]:
    """Tick de resolução — batch markets + redeem assíncrono em wins.

    ⚠️ Comparado ao loop antigo (1 req/position): faz UMA chamada
    batch via gamma.get_markets_batch e usa o BackgroundRateLimiter
    pra não competir com hot path.
    """
    stats = {"checked": 0, "won": 0, "lost": 0, "still_open": 0,
             "errors": 0, "redeems_dispatched": 0}

    async with conn.execute(
        "SELECT id, condition_id, token_id, outcome, size, avg_entry_price "
        "FROM bot_positions WHERE is_open=1"
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        return stats

    # Batch fetch — UMA chamada (ou poucas em chunks de 50). Limiter
    # dá o timing exato de 1 slot só pra este burst.
    unique_cids = list({r[1] for r in rows})
    bg = get_background_limiter()
    try:
        async with bg.acquire():
            markets_by_cid = await gamma.get_markets_batch(unique_cids)
    except Exception as exc:  # noqa: BLE001
        log.warning("resolution_batch_fetch_failed", err=repr(exc))
        markets_by_cid = {}

    # Resolve cada position contra o market batch ou via fallback.
    redeem_tasks: list[tuple[int, str, str, dict | None]] = []
    for row in rows:
        stats["checked"] += 1
        pid = row[0]
        cid = row[1]
        outcome = row[3]
        size = float(row[4])
        entry = float(row[5])
        market = markets_by_cid.get(cid.lower()) if cid else None

        result = await _resolve_via_market(
            conn, pid, cid, outcome, size, entry, market,
        )
        if result is None:
            stats["errors"] += 1
            continue
        status, _resolution_price = result
        if status == "won":
            stats["won"] += 1
            # Reserva slot redeem com sentinel "dispatching" — protege
            # contra dois ticks consecutivos disparando 2 tasks.
            res = await conn.execute(
                "UPDATE bot_positions SET redeem_tx_hash='dispatching' "
                "WHERE id=? AND redeem_tx_hash IS NULL",
                (pid,),
            )
            if res.rowcount > 0:
                redeem_tasks.append((pid, cid, outcome, market))
        elif status == "lost":
            stats["lost"] += 1
            # Sem redeem — tokens valem 0, gas seria desperdício.
            await conn.execute(
                "UPDATE bot_positions SET redeem_tx_hash='lost-skip' WHERE id=?",
                (pid,),
            )
        elif status == "still_open":
            stats["still_open"] += 1

    if stats["won"] or stats["lost"]:
        await conn.commit()

    # Spawn redeems fire-and-forget. Cada task atualiza tx_hash quando
    # confirma on-chain.
    for pid, cid, outcome, market in redeem_tasks:
        asyncio.create_task(
            _dispatch_redeem(conn, ctf, pid, cid, outcome, market, notifier),
            name=f"redeem-{pid}",
        )
        stats["redeems_dispatched"] += 1

    return stats


async def resolution_check_loop(
    *,
    shutdown: asyncio.Event,
    conn: aiosqlite.Connection,
    gamma: GammaAPIClient,
    ctf: CTFClient | None = None,
    notifier: object | None = None,
) -> None:
    while not shutdown.is_set():
        try:
            stats = await check_resolutions_once(conn, gamma, ctf=ctf, notifier=notifier)
            if stats["checked"]:
                log.info("resolution_check_tick", **stats)
        except Exception as exc:  # noqa: BLE001
            log.error("resolution_loop_crash", err=repr(exc))
        try:
            await asyncio.wait_for(
                shutdown.wait(), timeout=RESOLUTION_CHECK_INTERVAL_SECONDS,
            )
        except asyncio.TimeoutError:
            pass
