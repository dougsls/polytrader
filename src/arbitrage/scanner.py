"""Arbitrage scanner — varre mercados continuamente buscando YES+NO < 1.

Algoritmo:
  1. Lista mercados ativos via Gamma (paginação, filtra duração)
  2. Para cada mercado binário (2 outcomes), busca best ask de YES e NO
  3. Calcula edge bruto = 1 - (ask_yes + ask_no)
  4. Calcula edge líquido = bruto - fees - safety_buffer
  5. Mede profundidade de book em cada side
  6. Se edge_net >= min_edge_pct AND depth >= min_book_depth → emite ArbOpportunity

A oportunidade é serializada em arb_opportunities (status=pending) e
empurrada para a queue do executor. Scanner não posta ordens — apenas
detecta + grava + enfileira.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from src.api.clob_client import CLOBClient
from src.api.gamma_client import GammaAPIClient
from src.arbitrage.models import ArbOpportunity
from src.core.config import ArbitrageConfig
from src.core.logger import get_logger

log = get_logger(__name__)

# Concurrency para buscar books — Polymarket aguenta ~50 req/s.
# 20 paralelo é seguro e não bloqueia o tracker.
_BOOK_FETCH_CONCURRENCY = 20


def _depth_usd(book: dict[str, Any], side: str, max_levels: int) -> tuple[float, float]:
    """Retorna (best_price, depth_usd) para um lado do book.

    `side="asks"` (compra) ou `side="bids"` (venda). Soma `size * price`
    em até `max_levels` níveis. Retorna (0, 0) se book vazio.

    Polymarket book schema: {"bids": [{"price": "0.32", "size": "150"}, ...],
                             "asks": [{"price": "0.34", "size": "120"}, ...]}
    """
    levels = book.get(side) or []
    if not levels:
        return (0.0, 0.0)
    # Asks: ascending price (best = lowest). Bids: descending (best = highest).
    # Polymarket retorna os arrays já ordenados; defensive sort here:
    parsed = [(float(lvl["price"]), float(lvl["size"])) for lvl in levels[:max_levels]]
    if side == "asks":
        parsed.sort(key=lambda x: x[0])
    else:
        parsed.sort(key=lambda x: -x[0])
    best_price = parsed[0][0]
    depth = sum(p * s for p, s in parsed)
    return (best_price, depth)


class ArbScanner:
    def __init__(
        self,
        cfg: ArbitrageConfig,
        gamma: GammaAPIClient,
        clob: CLOBClient,
        queue: asyncio.Queue[ArbOpportunity],
        conn: aiosqlite.Connection,
    ) -> None:
        self.cfg = cfg
        self.gamma = gamma
        self.clob = clob
        self.queue = queue
        self.conn = conn
        self._semaphore = asyncio.Semaphore(_BOOK_FETCH_CONCURRENCY)
        # Cooldown por condition_id — evita reemitir mesma op se executor
        # ainda está processando.
        self._last_emitted: dict[str, datetime] = {}

    async def run_loop(self, shutdown: asyncio.Event) -> None:
        if not self.cfg.enabled:
            log.info("arb_scanner_disabled")
            return
        log.info(
            "arb_scanner_started",
            interval_s=self.cfg.scan_interval_seconds,
            min_edge=self.cfg.min_edge_pct,
        )
        while not shutdown.is_set():
            try:
                await self._sweep_once()
            except Exception as exc:  # noqa: BLE001
                log.warning("arb_sweep_failed", err=repr(exc))
            try:
                await asyncio.wait_for(
                    shutdown.wait(), timeout=self.cfg.scan_interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def _sweep_once(self) -> None:
        markets = await self.gamma.list_active_markets(
            limit=200,
            max_hours_to_resolution=self.cfg.max_hours_to_resolution,
            min_minutes_to_resolution=self.cfg.min_minutes_to_resolution,
        )
        log.info("arb_sweep_markets", count=len(markets))
        if not markets:
            return

        tasks = [self._evaluate_market(m) for m in markets]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        emitted = 0
        for r in results:
            if isinstance(r, ArbOpportunity):
                if await self._maybe_emit(r):
                    emitted += 1
            elif isinstance(r, Exception):
                log.debug("arb_eval_error", err=repr(r))
        if emitted:
            log.info("arb_opportunities_emitted", count=emitted)

    async def _evaluate_market(self, market: dict[str, Any]) -> ArbOpportunity | None:
        tokens = market.get("tokens") or []
        if len(tokens) != 2:
            return None  # multi-outcome (neg-risk) tratado em fase 2
        cond_id = market.get("conditionId") or market.get("condition_id")
        if not cond_id:
            return None
        # Cooldown: se esse cond foi emitido há menos que same_market_cooldown_s, pula.
        last = self._last_emitted.get(cond_id)
        now = datetime.now(timezone.utc)
        if last:
            age = (now - last).total_seconds()
            if age < self.cfg.same_market_cooldown_seconds:
                return None

        yes_id = tokens[0]["token_id"]
        no_id = tokens[1]["token_id"]
        # Identifica qual é "Yes" se possível — só pra label, não muda matemática
        if str(tokens[0].get("outcome", "")).lower().startswith("no"):
            yes_id, no_id = no_id, yes_id

        async with self._semaphore:
            try:
                book_yes, book_no = await asyncio.gather(
                    self.clob.book(yes_id),
                    self.clob.book(no_id),
                )
            except Exception as exc:  # noqa: BLE001 — book pode falhar pra mercados ilíquidos
                log.debug("arb_book_failed", cond_id=cond_id, err=repr(exc))
                return None

        ask_yes, depth_yes = _depth_usd(book_yes, "asks", max_levels=5)
        ask_no, depth_no = _depth_usd(book_no, "asks", max_levels=5)

        if ask_yes <= 0 or ask_no <= 0:
            return None  # sem ofertas em algum lado

        edge_gross = 1.0 - (ask_yes + ask_no)
        # 2 legs ⇒ 2× fee_per_leg. + safety_buffer.
        edge_net = edge_gross - (2 * self.cfg.fee_per_leg) - self.cfg.safety_buffer_pct
        if edge_net < self.cfg.min_edge_pct:
            return None
        if depth_yes < self.cfg.min_book_depth_usd or depth_no < self.cfg.min_book_depth_usd:
            return None

        # Tamanho recomendado: limitado por depth de cada side (best price level)
        # E pelo cap por op. Conservador: 50% do depth do lado mais raso.
        weakest_depth = min(depth_yes, depth_no)
        size_usd = min(self.cfg.max_per_op_usd, weakest_depth * 0.5)
        if size_usd < 1.0:
            return None

        op = ArbOpportunity(
            id=str(uuid.uuid4()),
            condition_id=str(cond_id),
            yes_token_id=yes_id,
            no_token_id=no_id,
            market_title=market.get("question") or market.get("title"),
            ask_yes=ask_yes,
            ask_no=ask_no,
            depth_yes_usd=depth_yes,
            depth_no_usd=depth_no,
            edge_gross_pct=edge_gross,
            edge_net_pct=edge_net,
            suggested_size_usd=size_usd,
            hours_to_resolution=market.get("_hours_to_resolution"),
            detected_at=now,
        )
        return op

    async def _maybe_emit(self, op: ArbOpportunity) -> bool:
        """Persiste em arb_opportunities e enfileira para o executor.

        Idempotência: se já existe uma op pending para o mesmo
        condition_id nos últimos cooldown_s, ignora.
        """
        await self.conn.execute(
            "INSERT INTO arb_opportunities("
            " id, condition_id, yes_token_id, no_token_id, market_title,"
            " ask_yes, ask_no, depth_yes_usd, depth_no_usd,"
            " edge_gross_pct, edge_net_pct, suggested_size_usd,"
            " hours_to_resolution, detected_at, status"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                op.id, op.condition_id, op.yes_token_id, op.no_token_id,
                op.market_title, op.ask_yes, op.ask_no, op.depth_yes_usd,
                op.depth_no_usd, op.edge_gross_pct, op.edge_net_pct,
                op.suggested_size_usd, op.hours_to_resolution,
                op.detected_at.isoformat(), op.status,
            ),
        )
        await self.conn.commit()
        try:
            self.queue.put_nowait(op)
        except asyncio.QueueFull:
            log.warning("arb_queue_full", cond_id=op.condition_id)
            return False
        self._last_emitted[op.condition_id] = op.detected_at
        log.info(
            "arb_opportunity",
            cond_id=op.condition_id,
            edge_net=f"{op.edge_net_pct:.4f}",
            ask_yes=f"{op.ask_yes:.4f}",
            ask_no=f"{op.ask_no:.4f}",
            size_usd=f"{op.suggested_size_usd:.2f}",
            title=(op.market_title or "")[:60],
        )
        return True
