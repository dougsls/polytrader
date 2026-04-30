"""Arbitrage scanner — varre mercados continuamente buscando YES+NO < 1.

QUANT — VWAP-based edge (corrige falso-positivo do Top of Book):

  Antes: edge_gross = 1 - (best_ask_yes + best_ask_no). PROBLEMA: o
  best_ask cobre só o L1 do book; um lote de $500 em book com $5 no
  L1 consome L2/L3 a preços piores → execução real diverge do edge
  estimado, FOK reverte ou capital fica preso.

  Agora: edge_gross = 1 - (vwap_yes + vwap_no). O VWAP é calculado
  caminhando o livro nível-a-nível até preencher o tamanho ALVO da
  arbitragem. É o preço médio REAL de execução. Falsos positivos
  causados por book raso são eliminados na origem.

Algoritmo:
  1. Lista mercados ativos via Gamma (paginação, filtra duração).
  2. Para cada mercado binário (2 outcomes), busca o livro de YES+NO.
  3. **Estimate pass**: walk infinito → descobre depth_total_tokens.
  4. **Resolve pass**: target = min(max_per_op_usd / best_combined,
     depth_yes, depth_no). Re-walk → vwap_yes, vwap_no.
  5. edge_gross = 1 - (vwap_yes + vwap_no).
  6. edge_net = edge_gross - 2×fee - safety_buffer.
  7. Se edge_net >= min_edge_pct AND fill ≥ min_book_depth_usd → emite.

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
    """[DEPRECATED — kept for backward compat com tests legados]

    Retorna (best_price, depth_usd_total). Para edge calculation usar
    `_walk_vwap` que retorna VWAP correto por target_size.

    Polymarket book schema: {"bids": [{"price": "0.32", "size": "150"}, ...],
                             "asks": [{"price": "0.34", "size": "120"}, ...]}
    """
    levels = book.get(side) or []
    if not levels:
        return (0.0, 0.0)
    parsed = [(float(lvl["price"]), float(lvl["size"])) for lvl in levels[:max_levels]]
    if side == "asks":
        parsed.sort(key=lambda x: x[0])
    else:
        parsed.sort(key=lambda x: -x[0])
    best_price = parsed[0][0]
    depth = sum(p * s for p, s in parsed)
    return (best_price, depth)


def _best_ask_l1(book: dict[str, Any]) -> float:
    """L1 ask — preço mais baixo no lado de venda. 0.0 se vazio."""
    asks = book.get("asks") or []
    if not asks:
        return 0.0
    # Polymarket retorna sorted asc; defensive min().
    return min(float(a["price"]) for a in asks)


def _walk_vwap(
    book: dict[str, Any],
    side: str,
    target_tokens: float,
    max_levels: int,
) -> tuple[float, float, float, int]:
    """Walk no order book consumindo liquidez até `target_tokens`.

    QUANT — núcleo da correção de edge:
      Para um lote de N tokens, o preço efetivo é o VWAP do path:
        VWAP = sum(price_i × tokens_taken_i) / sum(tokens_taken_i)
      Onde `tokens_taken_i = min(level_size_i, remaining)` para cada
      nível percorrido. Se o L1 não enche, vai pro L2 com preço pior.

    Args:
        book: schema Polymarket {"bids": [...], "asks": [...]}.
        side: "asks" para BUY, "bids" para SELL.
        target_tokens: quantos tokens queremos preencher. Use float("inf")
            para discovery do depth total.
        max_levels: cap em níveis percorridos. Limita queue tail
            ilíquida (defensive contra books com 50+ níveis poeira).

    Returns:
        (vwap, fillable_tokens, depth_usd_consumed, levels_used)
        - vwap: preço médio do path. 0.0 se book vazio.
        - fillable_tokens: efetivamente preenchível ≤ target.
        - depth_usd_consumed: vwap × fillable_tokens.
        - levels_used: níveis percorridos.
    """
    levels = book.get(side) or []
    if not levels:
        return (0.0, 0.0, 0.0, 0)
    parsed = [(float(lvl["price"]), float(lvl["size"])) for lvl in levels[:max_levels]]
    # Asks: ascending (best = lowest). Bids: descending (best = highest).
    if side == "asks":
        parsed.sort(key=lambda x: x[0])
    else:
        parsed.sort(key=lambda x: -x[0])

    remaining = target_tokens
    consumed_tokens = 0.0
    consumed_usd = 0.0
    levels_used = 0
    for price, size in parsed:
        if remaining <= 0:
            break
        take = size if size <= remaining else remaining
        consumed_tokens += take
        consumed_usd += take * price
        remaining -= take
        levels_used += 1

    if consumed_tokens <= 0:
        return (0.0, 0.0, 0.0, 0)
    vwap = consumed_usd / consumed_tokens
    return (vwap, consumed_tokens, consumed_usd, levels_used)


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

        # === QUANT — VWAP-based edge =====================================
        # Pass 1 (depth survey): walk infinito → descobre depth total
        # disponível em tokens. Este `vwap` retornado é o VWAP COMPLETO
        # do livro (não usado para edge — só pra log/debug).
        max_lv = 5
        _, depth_yes_tokens, depth_yes_usd, _ = _walk_vwap(
            book_yes, "asks", target_tokens=float("inf"), max_levels=max_lv,
        )
        _, depth_no_tokens, depth_no_usd, _ = _walk_vwap(
            book_no, "asks", target_tokens=float("inf"), max_levels=max_lv,
        )
        # Best L1 (top of book) — usado APENAS para estimar quantos
        # tokens cabem em max_per_op_usd no melhor cenário possível.
        best_l1_yes = _best_ask_l1(book_yes)
        best_l1_no = _best_ask_l1(book_no)
        if best_l1_yes <= 0 or best_l1_no <= 0:
            return None  # sem ofertas em algum lado
        if depth_yes_tokens <= 0 or depth_no_tokens <= 0:
            return None

        # Pass 2 (resolve): target em tokens limitado por max_per_op_usd
        # (calculado ao MELHOR PREÇO L1 como upper bound otimista) E pelo
        # depth real em cada side.
        upper_target_tokens = self.cfg.max_per_op_usd / (best_l1_yes + best_l1_no)
        effective_target = min(upper_target_tokens, depth_yes_tokens, depth_no_tokens)
        # 50% do depth do lado mais raso — conservador, deixa folga pra
        # outras execuções concorrentes não consumirem o book inteiro.
        effective_target *= 0.5
        if effective_target <= 0:
            return None

        # Re-walk com o tamanho efetivo → VWAP REAL de execução.
        vwap_yes, fillable_yes, cost_yes_usd, lv_yes = _walk_vwap(
            book_yes, "asks", target_tokens=effective_target, max_levels=max_lv,
        )
        vwap_no, fillable_no, cost_no_usd, lv_no = _walk_vwap(
            book_no, "asks", target_tokens=effective_target, max_levels=max_lv,
        )
        # Sanity — algum lado pode ter encolhido entre os 2 walks (paranoia)
        if vwap_yes <= 0 or vwap_no <= 0 or fillable_yes <= 0 or fillable_no <= 0:
            return None

        # Edge baseado em VWAP, não em best_ask. Isso elimina o falso
        # positivo: se L1 era ilusório, vwap reflete o consumo real.
        edge_gross = 1.0 - (vwap_yes + vwap_no)
        edge_net = edge_gross - (2 * self.cfg.fee_per_leg) - self.cfg.safety_buffer_pct
        if edge_net < self.cfg.min_edge_pct:
            return None

        # Cost real em USDC para o par YES+NO (size_pares × (vwap_y+vwap_n))
        effective_pairs = min(fillable_yes, fillable_no)  # par = 1 YES + 1 NO
        size_usd = effective_pairs * (vwap_yes + vwap_no)
        if size_usd < 1.0:
            return None
        if depth_yes_usd < self.cfg.min_book_depth_usd or depth_no_usd < self.cfg.min_book_depth_usd:
            return None

        log.debug(
            "arb_vwap_resolved",
            cond_id=cond_id, vwap_yes=vwap_yes, vwap_no=vwap_no,
            best_l1_yes=best_l1_yes, best_l1_no=best_l1_no,
            levels_yes=lv_yes, levels_no=lv_no,
            cost_yes=cost_yes_usd, cost_no=cost_no_usd,
            target_tokens=effective_target,
        )

        # ArbOpportunity.ask_yes/ask_no agora carregam VWAP (preço efetivo
        # de execução do tamanho recomendado) — semântica explícita pro
        # executor que vai cotizar/postar ordens nesse preço.
        op = ArbOpportunity(
            id=str(uuid.uuid4()),
            condition_id=str(cond_id),
            yes_token_id=yes_id,
            no_token_id=no_id,
            market_title=market.get("question") or market.get("title"),
            ask_yes=vwap_yes,
            ask_no=vwap_no,
            depth_yes_usd=depth_yes_usd,
            depth_no_usd=depth_no_usd,
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
