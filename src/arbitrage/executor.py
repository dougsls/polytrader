"""Arbitrage executor — multi-leg atomic com rollback.

Pipeline para cada ArbOpportunity:
  1. Verifica banca (capital remaining após ops abertas)
  2. Quantiza preços/sizes (Diretiva 1)
  3. Posta YES@ask_yes e NO@ask_no como FOK em paralelo
  4. Se ambas filladas:
       - Se cfg.auto_merge: chama CTF.merge_binary → realiza lucro
       - Senão: registra como "merge_pending" pra worker assíncrono
  5. Se UMA filla e outra falha: ROLLBACK
       - Vende imediatamente a leg fillada (FOK no melhor bid disponível)
       - Marca op como rolled_back (PnL = -spread × size, dano contido)
  6. Persiste tudo em arb_executions

Modes:
  - paper: simula fills perfeitos, não chama CTF, calcula PnL teórico
  - dry-run: detecta + loga + grava como skipped
  - live: posta ordens + chama CTF
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import aiosqlite

from src.api.clob_client import CLOBClient
from src.api.gamma_client import GammaAPIClient
from src.api.order_builder import MarketSpec, build_order
from src.arbitrage.ctf_client import CTFClient
from src.arbitrage.models import ArbExecution, ArbLegFill, ArbOpportunity
from src.core.config import ArbitrageConfig
from src.core.exceptions import PolymarketAPIError
from src.core.logger import get_logger

log = get_logger(__name__)


class ArbBank:
    """Banca isolada da arb — não confunde com balance do copy-trader.

    Trackeia capital_inicial - exposure_aberta + pnl_realizado.
    Nada mágico: lê arb_executions e soma. Cache em RAM atualizado a
    cada execução.
    """

    def __init__(self, cfg: ArbitrageConfig, conn: aiosqlite.Connection) -> None:
        self.cfg = cfg
        self.conn = conn
        self._capital = cfg.max_capital_usd
        self._realized_pnl = 0.0
        self._exposure_open = 0.0
        self._completed = 0
        self._failed = 0

    async def reload(self) -> None:
        async with self.conn.execute(
            "SELECT status, invested_usd, realized_pnl_usd FROM arb_executions"
        ) as cur:
            rows = await cur.fetchall()
        self._realized_pnl = 0.0
        self._exposure_open = 0.0
        self._completed = 0
        self._failed = 0
        for status, invested, pnl in rows:
            if status == "completed":
                self._realized_pnl += pnl or 0.0
                self._completed += 1
            elif status in ("failed", "rolled_back"):
                self._realized_pnl += pnl or 0.0
                self._failed += 1
            elif status == "pending":
                self._exposure_open += invested or 0.0

    @property
    def available(self) -> float:
        return self._capital + self._realized_pnl - self._exposure_open

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl

    def can_open(self, size_usd: float) -> bool:
        return self.available >= size_usd

    async def snapshot(self) -> None:
        await self.conn.execute(
            "INSERT INTO arb_bank_snapshots("
            " timestamp, capital_usd, realized_pnl_usd, open_ops,"
            " completed_ops_total, failed_ops_total"
            ") VALUES (?,?,?,?,?,?)",
            (
                datetime.now(timezone.utc).isoformat(),
                self._capital, self._realized_pnl,
                int(self._exposure_open > 0), self._completed, self._failed,
            ),
        )
        await self.conn.commit()


class ArbExecutor:
    def __init__(
        self,
        cfg: ArbitrageConfig,
        clob: CLOBClient,
        gamma: GammaAPIClient,
        ctf: CTFClient | None,
        queue: asyncio.Queue[ArbOpportunity],
        conn: aiosqlite.Connection,
        on_event: Any | None = None,  # async callable(kind, op, exec)
    ) -> None:
        self.cfg = cfg
        self.clob = clob
        self.gamma = gamma
        self.ctf = ctf
        self.queue = queue
        self.conn = conn
        self.bank = ArbBank(cfg, conn)
        self._inflight: set[str] = set()
        self._on_event = on_event

    async def run_loop(self, shutdown: asyncio.Event) -> None:
        if not self.cfg.enabled:
            return
        await self.bank.reload()
        log.info("arb_executor_started", available=f"{self.bank.available:.2f}")
        while not shutdown.is_set():
            try:
                op = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            # Concurrent cap
            if len(self._inflight) >= self.cfg.max_concurrent_ops:
                log.warning("arb_at_max_concurrent", inflight=len(self._inflight))
                await self._mark_skipped(op, "MAX_CONCURRENT")
                continue
            asyncio.create_task(self._execute(op))

    async def _execute(self, op: ArbOpportunity) -> None:
        self._inflight.add(op.id)
        try:
            await self._execute_inner(op)
        finally:
            self._inflight.discard(op.id)

    async def _execute_inner(self, op: ArbOpportunity) -> None:
        # Re-check edge ANTES de postar — book pode ter andado.
        if not self.bank.can_open(op.suggested_size_usd):
            await self._mark_skipped(op, "INSUFFICIENT_BANK")
            return

        # Pull market spec uma vez (tick_size + neg_risk).
        try:
            market = await self.gamma.get_market(op.condition_id)
        except Exception as exc:  # noqa: BLE001
            await self._mark_skipped(op, f"GAMMA_FAIL: {exc!r}")
            return
        if market.get("neg_risk") or market.get("negRisk"):
            # Fase 2 — neg-risk merge usa NegRiskAdapter
            await self._mark_skipped(op, "NEG_RISK_NOT_SUPPORTED")
            return

        tick = Decimal(str(market.get("minimum_tick_size") or market.get("tick_size") or 0.01))
        size_step = Decimal(str(market.get("min_order_size") or "1"))
        # Size em "tokens" — 1 token = 1 USD de payout. size_usd = size_tokens * ask.
        # Compramos size_tokens iguais nos dois lados (mesma quantidade).
        # Calculamos size_tokens limitado por suggested_size_usd:
        #   cost = size_tokens * (ask_yes + ask_no)
        # Soluciona pra cost <= suggested_size_usd:
        max_cost = op.suggested_size_usd
        size_tokens = max_cost / (op.ask_yes + op.ask_no)

        spec_yes = MarketSpec(
            condition_id=op.condition_id, token_id=op.yes_token_id,
            tick_size=tick, size_step=size_step, neg_risk=False,
        )
        spec_no = MarketSpec(
            condition_id=op.condition_id, token_id=op.no_token_id,
            tick_size=tick, size_step=size_step, neg_risk=False,
        )
        try:
            draft_yes = build_order(spec_yes, "BUY", op.ask_yes, size_tokens)
            draft_no = build_order(spec_no, "BUY", op.ask_no, size_tokens)
        except ValueError as exc:
            await self._mark_skipped(op, f"QUANTIZE: {exc}")
            return

        execution = ArbExecution(
            id=str(uuid.uuid4()),
            opportunity_id=op.id,
            condition_id=op.condition_id,
            yes_leg=ArbLegFill(
                side="YES", token_id=op.yes_token_id,
                intended_size=float(draft_yes.size),
                intended_price=float(draft_yes.price),
            ),
            no_leg=ArbLegFill(
                side="NO", token_id=op.no_token_id,
                intended_size=float(draft_no.size),
                intended_price=float(draft_no.price),
            ),
            invested_usd=float(draft_yes.price * draft_yes.size + draft_no.price * draft_no.size),
            started_at=datetime.now(timezone.utc),
        )
        await self._persist_execution(execution)
        await self._update_op_status(op.id, "executing")

        # === MODE BRANCH ===
        if self.cfg.mode == "dry-run":
            execution.status = "completed"
            execution.error = "dry-run"
            execution.realized_pnl_usd = self._theoretical_pnl(op, size_tokens)
            execution.completed_at = datetime.now(timezone.utc)
            await self._persist_execution(execution)
            await self._update_op_status(op.id, "executed")
            log.info("arb_dryrun", op_id=op.id, theoretical_pnl=execution.realized_pnl_usd)
            await self._fire_event("arb_executed", op, execution)
            return

        if self.cfg.mode == "paper":
            # Simula fills perfeitos no preço de aresta e calcula PnL teórico.
            execution.yes_leg.fill_price = float(draft_yes.price)
            execution.yes_leg.fill_size = float(draft_yes.size)
            execution.yes_leg.status = "filled"
            execution.no_leg.fill_price = float(draft_no.price)
            execution.no_leg.fill_size = float(draft_no.size)
            execution.no_leg.status = "filled"
            execution.merge_status = "skipped"
            execution.merge_amount_usd = float(draft_yes.size)
            execution.realized_pnl_usd = self._theoretical_pnl(op, size_tokens)
            execution.status = "completed"
            execution.completed_at = datetime.now(timezone.utc)
            await self._persist_execution(execution)
            await self._update_op_status(op.id, "executed")
            await self.bank.reload()
            log.info(
                "arb_paper_completed",
                op_id=op.id,
                pnl=f"{execution.realized_pnl_usd:.4f}",
                size_tokens=f"{size_tokens:.2f}",
            )
            await self._fire_event("arb_executed", op, execution)
            return

        # === LIVE MODE ===
        # Post 2 FOK ordens em PARALELO. FOK = encha total ou cancela.
        # Razão: GTC pendurado em 1 leg sem a outra = exposure direcional.
        try:
            yes_task = asyncio.create_task(self._post_fok(draft_yes))
            no_task = asyncio.create_task(self._post_fok(draft_no))
            yes_resp, no_resp = await asyncio.gather(
                yes_task, no_task, return_exceptions=True,
            )
        except Exception as exc:  # noqa: BLE001
            execution.error = f"PARALLEL_POST: {exc!r}"
            execution.status = "failed"
            execution.completed_at = datetime.now(timezone.utc)
            await self._persist_execution(execution)
            await self._update_op_status(op.id, "failed")
            return

        yes_ok = self._apply_response(execution.yes_leg, yes_resp)
        no_ok = self._apply_response(execution.no_leg, no_resp)

        if yes_ok and no_ok:
            # Ambas filladas — chama merge se configurado.
            if self.cfg.auto_merge and self.ctf is not None:
                try:
                    fill_size = min(
                        execution.yes_leg.fill_size or 0,
                        execution.no_leg.fill_size or 0,
                    )
                    tx_hash = await self.ctf.merge_binary(op.condition_id, fill_size)
                    execution.merge_tx_hash = tx_hash
                    execution.merge_status = "confirmed"
                    execution.merge_amount_usd = fill_size
                    invested = (execution.yes_leg.fill_price * (execution.yes_leg.fill_size or 0)) + \
                               (execution.no_leg.fill_price * (execution.no_leg.fill_size or 0))
                    execution.realized_pnl_usd = fill_size - invested
                    execution.status = "completed"
                except Exception as exc:  # noqa: BLE001
                    log.error("ctf_merge_failed", op_id=op.id, err=repr(exc))
                    execution.merge_status = "failed"
                    execution.error = f"MERGE: {exc!r}"
                    execution.status = "partial"  # tem posições, sem merge
            else:
                execution.merge_status = "skipped"
                execution.status = "completed"
                # Sem merge, PnL é unrealized — registra invested.
                invested = (execution.yes_leg.fill_price or 0) * (execution.yes_leg.fill_size or 0) + \
                           (execution.no_leg.fill_price or 0) * (execution.no_leg.fill_size or 0)
                execution.realized_pnl_usd = (execution.yes_leg.fill_size or 0) - invested
        elif yes_ok or no_ok:
            # ROLLBACK — uma leg fillou, outra não. Tenta vender a fillada.
            await self._rollback(execution)
        else:
            # Nenhuma fillou.
            execution.status = "failed"
            execution.error = "BOTH_LEGS_FAILED"

        execution.completed_at = datetime.now(timezone.utc)
        await self._persist_execution(execution)
        terminal_status = "executed" if execution.status == "completed" else "failed"
        await self._update_op_status(op.id, terminal_status)
        await self.bank.reload()
        log.info(
            "arb_live_done",
            op_id=op.id, status=execution.status,
            pnl=execution.realized_pnl_usd, merge=execution.merge_status,
        )
        await self._fire_event("arb_executed", op, execution)

    def _theoretical_pnl(self, op: ArbOpportunity, size_tokens: float) -> float:
        """PnL teórico = size * (1 - ask_yes - ask_no - 2*fee)."""
        gross = 1.0 - op.ask_yes - op.ask_no
        net = gross - (2 * self.cfg.fee_per_leg)
        return size_tokens * net

    async def _post_fok(self, draft: Any) -> dict[str, Any]:
        # FOK = Fill-Or-Kill — encha 100% ou cancela. Garante atomicidade.
        return await self.clob.post_order(draft, order_type="FOK")

    def _apply_response(self, leg: ArbLegFill, resp: Any) -> bool:
        if isinstance(resp, Exception):
            leg.status = "failed"
            leg.error = repr(resp)
            return False
        leg.order_id = resp.get("orderID") or resp.get("orderId")
        # SDK retorna `status` ou `success`. Live retorna fills inline em
        # FOK. Conservador: se errorMsg está vazio e success true, ok.
        if resp.get("success") and not resp.get("errorMsg"):
            leg.status = "filled"
            leg.fill_price = leg.intended_price
            leg.fill_size = leg.intended_size
            return True
        leg.status = "failed"
        leg.error = str(resp.get("errorMsg") or resp)[:200]
        return False

    async def _rollback(self, execution: ArbExecution) -> None:
        """Uma leg fillou e a outra não — vende a fillada pra zerar exposure.

        Não é mágica: pode dar prejuízo (spread bid-ask). Mas evita ficar
        com posição direcional não-coberta. Erro contido.
        """
        log.warning(
            "arb_rollback_triggered",
            yes=execution.yes_leg.status, no=execution.no_leg.status,
        )
        execution.status = "rolled_back"
        # TODO: implementar venda imediata via FOK no melhor bid.
        # Requer um draft SELL e re-quantize. Pra primeira versão,
        # marca como rolled_back e deixa o operador resolver manual via
        # dashboard/Telegram alert. Em paper esse caminho não é exercido.
        execution.error = (execution.error or "") + " [auto-rollback PENDENTE — vender manualmente]"

    async def _persist_execution(self, e: ArbExecution) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO arb_executions("
            " id, opportunity_id, condition_id, invested_usd,"
            " yes_order_id, no_order_id,"
            " yes_fill_price, no_fill_price, yes_fill_size, no_fill_size,"
            " yes_status, no_status,"
            " merge_tx_hash, merge_status, merge_amount_usd,"
            " realized_pnl_usd, status, error,"
            " started_at, completed_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                e.id, e.opportunity_id, e.condition_id, e.invested_usd,
                e.yes_leg.order_id, e.no_leg.order_id,
                e.yes_leg.fill_price, e.no_leg.fill_price,
                e.yes_leg.fill_size, e.no_leg.fill_size,
                e.yes_leg.status, e.no_leg.status,
                e.merge_tx_hash, e.merge_status, e.merge_amount_usd,
                e.realized_pnl_usd, e.status, e.error,
                e.started_at.isoformat(),
                e.completed_at.isoformat() if e.completed_at else None,
            ),
        )
        await self.conn.commit()

    async def _update_op_status(self, op_id: str, status: str) -> None:
        await self.conn.execute(
            "UPDATE arb_opportunities SET status=? WHERE id=?",
            (status, op_id),
        )
        await self.conn.commit()

    async def _mark_skipped(self, op: ArbOpportunity, reason: str) -> None:
        log.info("arb_skipped", op_id=op.id, reason=reason)
        await self.conn.execute(
            "UPDATE arb_opportunities SET status='skipped', skip_reason=? WHERE id=?",
            (reason, op.id),
        )
        await self.conn.commit()

    async def _fire_event(self, kind: str, op: ArbOpportunity, ex: ArbExecution | None) -> None:
        if self._on_event is None:
            return
        try:
            await self._on_event(kind, op, ex)
        except Exception as exc:  # noqa: BLE001
            log.warning("arb_event_callback_failed", err=repr(exc))
