"""Copy engine — pipeline central de execução.

Sequência (todas as leis do mercado aplicadas aqui, em ordem):
    1. RiskManager.evaluate      — gates de capital
    2. check_slippage_or_abort   — Regra 1 (Anti-Slippage Anchoring)
    3. order_manager.build_draft — quantização + neg_risk (Diretivas 1 e 4)
    4. CLOB.post_order           — em mode=live; senão simula fill
    5. position_manager.apply_fill
    6. notify via callback

Todo sinal que atravessa o handle_signal termina com `trade_signals.status`
atualizado (executed/skipped/failed) e com skip_reason auditável.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from src.api.clob_client import CLOBClient
from src.api.gamma_client import GammaAPIClient
from src.core.config import ExecutorConfig
from src.core.database import DEFAULT_DB_PATH, get_connection
from src.core.exceptions import (
    PolymarketAPIError,
    SlippageExceededError,
    SpreadTooWideError,
)
from src.core.logger import get_logger
from src.core import metrics
from src.core.models import CopyTrade, RiskState, TradeSignal
from src.core.state import InMemoryState
from src.executor.exposure import would_breach_tag_cap
from src.executor.order_manager import build_draft, persist_trade_async
from src.executor.order_watchdog import watchdog_order
from src.executor.position_manager import apply_fill
from src.executor.risk_manager import RiskManager
from src.executor.slippage import check_slippage_or_abort, compute_optimistic_ref_price

import aiosqlite

log = get_logger(__name__)

NotifyCallback = Callable[[str, TradeSignal, CopyTrade | None], Awaitable[None]] | None


class CopyEngine:
    def __init__(
        self,
        *,
        cfg: ExecutorConfig,
        clob: CLOBClient,
        gamma: GammaAPIClient,
        risk: RiskManager,
        queue: asyncio.Queue,
        state: InMemoryState,
        risk_state_provider: Callable[[], RiskState],
        on_event: NotifyCallback = None,
        db_path: Path = DEFAULT_DB_PATH,
        conn: aiosqlite.Connection | None = None,
    ) -> None:
        self._cfg = cfg
        self._clob = clob
        self._gamma = gamma
        self._risk = risk
        self._queue = queue
        self._state = state                      # InMemoryState (RAM cache)
        self._risk_state = risk_state_provider   # Callable[[], RiskState]
        self._on_event = on_event
        self._db_path = db_path
        self._conn = conn
        # HFT — Semaphore limita ordens em flight ao CLOB. NY→London 100ms
        # × 4 concurrent = ~40 ord/s, longe do rate limit Polymarket.
        max_conc = int(getattr(cfg, "max_concurrent_signals", 1) or 1)
        self._post_semaphore = asyncio.Semaphore(max_conc)
        # Lock guarda a SEÇÃO DE DECISIONING (risk gates + cash availability +
        # market cap). Sem isso, dois signals concorrentes podem ambos passar
        # pelo `cash_available > 1.0` e oversubscribir a banca antes do
        # primeiro `apply_fill` write-through. O lock é fino (cobre só a
        # decisão, não o post_order, que é o caminho longo).
        self._risk_lock = asyncio.Lock()

    async def _mark_skipped(self, signal: TradeSignal, reason: str) -> None:
        # Extrai classe do motivo (prefixo antes do ":") pra label baixa-cardinalidade
        reason_class = reason.split(":", 1)[0].strip() if ":" in reason else reason
        metrics.trades_skipped.labels(reason_class=reason_class).inc()
        # Fase 3 LOW — usa shared_conn em vez de abrir conexão nova.
        if self._conn is not None:
            await self._conn.execute(
                "UPDATE trade_signals SET status='skipped', skip_reason=? WHERE id=?",
                (reason, signal.id),
            )
            await self._conn.commit()
        else:
            async with get_connection(self._db_path) as db:
                await db.execute(
                    "UPDATE trade_signals SET status='skipped', skip_reason=? WHERE id=?",
                    (reason, signal.id),
                )
                await db.commit()
        log.info("signal_skipped", id=signal.id, reason=reason)
        if self._on_event:
            await self._on_event("skipped", signal, None)

    async def handle_signal(self, signal: TradeSignal) -> None:
        import time as _t
        _start = _t.perf_counter()

        # PAPER PERFECT MIRROR — bypass total dos filtros (paper observation only)
        perfect_mirror = (
            self._cfg.mode != "live"
            and getattr(self._cfg, "paper_perfect_mirror", False)
        )

        # === DECISION SECTION (lock-guarded) ============================
        # Risk gates + cash check + market cap são lidos+decididos atomically
        # pra evitar over-subscription de banca quando vários signals
        # processam concorrentemente. Lock é solto antes do post_order.
        async with self._risk_lock:
            decision_result = await self._make_decision(signal, perfect_mirror)
        if decision_result is None:
            return  # já marcou skipped dentro de _make_decision
        decision, ref_price = decision_result

        # Daqui pra frente é POST + apply_fill. Roda fora do lock.
        await self._submit_and_apply(
            signal=signal, decision=decision, ref_price=ref_price,
            perfect_mirror=perfect_mirror, start_perf=_start,
        )

    async def _make_decision(
        self, signal: TradeSignal, perfect_mirror: bool,
    ) -> tuple[Any, float] | None:
        """Calcula `decision.sized_usd` e `ref_price` ou retorna None se skip.

        Encapsula toda a lógica de decisioning (perfect_mirror sizing,
        risk gates, slippage anchor, tag exposure). Roda dentro do lock.
        """
        if perfect_mirror:
            # Realista: sizing respeita cash DISPONÍVEL (banca - capital
            # travado em posições abertas + realized PnL). Sem isso, o
            # paper simula investir $32k com banca de $50 — distorce toda
            # análise de viabilidade do bot em live.
            # SELL não consome caixa (é saída de token); permite sempre.
            starting_bank = float(self._cfg.max_portfolio_usd)
            cash_available = starting_bank
            active_bank = starting_bank   # default p/ SELL (sem leitura DB)
            max_per_market = 0.0
            remaining_market = 0.0
            if signal.side == "BUY" and self._conn is not None:
                async with self._conn.execute(
                    "SELECT COALESCE(SUM(size*avg_entry_price),0) "
                    "FROM bot_positions WHERE is_open=1"
                ) as cur:
                    row = await cur.fetchone()
                invested_open = float(row[0]) if row else 0.0
                # Separar lucros (wins) de perdas pra computar cofre.
                # Cofre acumula 30% dos wins; perdas descontam da banca ativa.
                async with self._conn.execute(
                    "SELECT COALESCE(SUM(CASE WHEN realized_pnl>0 THEN realized_pnl ELSE 0 END),0), "
                    "       COALESCE(SUM(CASE WHEN realized_pnl<0 THEN realized_pnl ELSE 0 END),0) "
                    "FROM bot_positions"
                ) as cur:
                    row = await cur.fetchone()
                positive_realized = float(row[0]) if row else 0.0
                negative_realized = float(row[1]) if row else 0.0
                safe_pct = float(getattr(self._cfg, "profit_safe_pct", 0.0) or 0.0)
                safe_bank = positive_realized * safe_pct
                active_bank = (starting_bank + positive_realized * (1 - safe_pct)
                               + negative_realized)
                cash_available = active_bank - invested_open
                # Circuit breaker: pausa se banca ativa caiu muito.
                min_pct = float(getattr(self._cfg, "min_active_bank_pct", 0.0) or 0.0)
                min_active = starting_bank * min_pct
                if min_pct > 0 and active_bank < min_active:
                    await self._mark_skipped(
                        signal,
                        f"BANK_DRAWDOWN_PAUSE: ativa ${active_bank:.2f} < min ${min_active:.2f} "
                        f"(cofre ${safe_bank:.2f} protegido)",
                    )
                    return None
                if cash_available < 1.0:
                    await self._mark_skipped(
                        signal,
                        f"INSUFFICIENT_CASH: ${cash_available:.2f} ativa (cofre ${safe_bank:.2f})",
                    )
                    return None
                # Cap cumulativo POR mercado: baseado em banca ATIVA
                # (sem contar cofre). Cofre não amplia risco.
                max_per_market = active_bank * float(
                    getattr(self._cfg, "max_position_pct_of_bank", 0.05) or 0.05
                )
                async with self._conn.execute(
                    "SELECT COALESCE(SUM(size*avg_entry_price),0) "
                    "FROM bot_positions WHERE is_open=1 AND token_id=?",
                    (signal.token_id,),
                ) as cur:
                    row = await cur.fetchone()
                already_in_market = float(row[0]) if row else 0.0
                remaining_market = max(0.0, max_per_market - already_in_market)
                if remaining_market < 1.0:
                    await self._mark_skipped(
                        signal,
                        f"MARKET_CAP: ${already_in_market:.2f} já alocado neste mercado "
                        f"(cap ${max_per_market:.2f} = {self._cfg.max_position_pct_of_bank:.0%} da banca)",
                    )
                    return None
            # === Perfect Mirror Sizing — proporcional à convicção da whale ===
            # Antes: target = starting_bank × proportional_factor (cego).
            # Agora: target = active_bank × (whale_trade_usd / whale_portfolio_usd) × factor.
            # Reproduz a MESMA % que a baleia alocou. Quando whale_portfolio_usd
            # está ausente (sinal sem enrich), cai no fallback histórico.
            whale_pct: float | None = None
            wp = float(signal.whale_portfolio_usd or 0.0)
            if wp > 0 and signal.usd_value > 0:
                whale_pct = min(signal.usd_value / wp, 1.0)
                base_bank = active_bank if signal.side == "BUY" else starting_bank
                target = base_bank * whale_pct * float(
                    getattr(self._cfg, "whale_sizing_factor", 1.0) or 1.0
                )
            else:
                target = starting_bank * self._cfg.proportional_factor
            sized = min(target, self._cfg.max_position_usd, cash_available)
            if signal.side == "BUY" and max_per_market > 0:
                sized = min(sized, remaining_market)
            sized = max(sized, 1.0)  # Polymarket mín $1
            log.info(
                "perfect_mirror_sized",
                signal_id=signal.id,
                whale_pct=whale_pct,
                target=target, sized=sized,
                base_bank=active_bank if signal.side == "BUY" else starting_bank,
            )

            class _D:
                allowed = True
                reason = "OK"
                sized_usd = sized
            decision = _D()
            ref_price = signal.price  # bypass slippage anchor — usa preço whale
            return decision, ref_price

        # === Non-perfect-mirror branch: risk gates + slippage anchoring ====
        # 1. Risk gates
        decision = self._risk.evaluate(signal, self._risk_state())
        if not decision.allowed:
            await self._mark_skipped(signal, f"RISK: {decision.reason}")
            return None

        # 2. Slippage anchoring.
        # ⚠️ DEATH-TRAP FIX: stale_cleanup signals carregam
        # `bypass_slippage_check=True`. Sinais reais de copy/arb
        # NUNCA setam isso — Regra 1 segue protegendo contra
        # comprar caro depois da whale ter movido o book.
        if signal.bypass_slippage_check:
            log.warning(
                "stale_force_sell_bypass_slippage",
                signal_id=signal.id, token_id=signal.token_id[:12],
                anchor=signal.price,
            )
            ref_price = signal.price
        elif getattr(self._cfg, "optimistic_execution", False):
            # Optimistic Execution: pula o pre-flight REST, embute
            # tolerance no limit_price (economiza ~80-130ms NY→London).
            try:
                ref_price = compute_optimistic_ref_price(
                    side=signal.side, whale_price=signal.price,
                    tolerance_pct=self._cfg.whale_max_slippage_pct,
                )
            except ValueError as exc:
                await self._mark_skipped(signal, f"OPTIMISTIC: {exc}")
                return None
        else:
            try:
                ref_price = await check_slippage_or_abort(
                    clob=self._clob,
                    token_id=signal.token_id,
                    side=signal.side,
                    whale_price=signal.price,
                    tolerance_pct=self._cfg.whale_max_slippage_pct,
                    max_spread=self._cfg.max_spread,
                )
            except SpreadTooWideError as exc:
                await self._mark_skipped(
                    signal, f"SPREAD: {exc.spread:.4f} > {exc.max_spread:.4f}"
                )
                return None
            except SlippageExceededError as exc:
                await self._mark_skipped(signal, f"SLIPPAGE: {exc.actual:.4f}")
                return None
            except PolymarketAPIError as exc:
                await self._mark_skipped(signal, f"BOOK_FETCH: {exc}")
                return None

        # 3. Tag exposure cap (BUY only).
        if signal.side == "BUY" and self._conn is not None:
            try:
                market = await self._gamma.get_market(signal.condition_id)
                risk_state = self._risk_state()
                breach, tag, pct = await would_breach_tag_cap(
                    conn=self._conn, market=market, new_usd=decision.sized_usd,
                    portfolio_value=risk_state.total_portfolio_value,
                    max_tag_exposure_pct=self._cfg.max_tag_exposure_pct,
                )
                if breach:
                    await self._mark_skipped(
                        signal, f"TAG_EXPOSURE: {tag}={pct:.1%}",
                    )
                    return None
            except Exception as exc:  # noqa: BLE001 — soft-fail
                log.warning("tag_exposure_check_failed", err=repr(exc))

        return decision, ref_price

    async def _submit_and_apply(
        self, *,
        signal: TradeSignal, decision: Any, ref_price: float,
        perfect_mirror: bool, start_perf: float,
    ) -> None:
        """Pós-decisão: build_draft (RAM) → post_order → persist DB async → apply_fill.

        HFT — Hot-Path:
            Sequência crítica: o INSERT em copy_trades NÃO bloqueia o
            envio da ordem. build_draft é puro RAM, post_order vai PRIMEIRO,
            depois `asyncio.create_task(persist_trade_async)` corre em
            paralelo com apply_fill. Disco SQLite (~1-3ms WAL) sai do
            caminho crítico. Em rajadas de 10 signals, isso poupa 10-30ms
            cumulativos do signal-to-fill.
        """
        import time as _t

        # 1. Build order — RAM only, sub-millisecond.
        draft, trade, _spec = await build_draft(
            signal=signal, sized_usd=decision.sized_usd, ref_price=ref_price,
            gamma=self._gamma, cfg=self._cfg, db_path=self._db_path,
        )

        executed_price = float(draft.price)
        executed_size = float(draft.size)
        # HFT — Optimistic Execution força FOK pra rejeição on-exchange
        # não deixar GTC pendurado (= exposure direcional sem cobertura).
        order_type = self._cfg.default_order_type
        if getattr(self._cfg, "optimistic_execution", False):
            order_type = "FOK"

        # 2. Submit ANTES do disco — Semaphore limita req/s ao CLOB.
        if self._cfg.mode == "live":
            async with self._post_semaphore:
                try:
                    post_resp = await self._clob.post_order(draft, order_type=order_type)
                    self._risk.record_post_success()
                    # FOK watchdog (só faz sentido para GTC pendurado).
                    if order_type != "FOK" and self._cfg.fok_fallback and post_resp:
                        order_id = post_resp.get("orderID") or post_resp.get("orderId") or ""
                        if order_id:
                            asyncio.create_task(watchdog_order(
                                clob=self._clob, order_id=order_id, draft=draft,
                                timeout_s=self._cfg.fok_fallback_timeout_seconds,
                            ), name=f"watchdog-{order_id[:8]}")
                except NotImplementedError:
                    log.error("live_mode_not_wired", signal_id=signal.id)
                    # DB persist com status final — fora do hot path.
                    trade.status = "failed"
                    trade.error = "LIVE_NOT_WIRED"
                    asyncio.create_task(persist_trade_async(
                        trade, conn=self._conn, db_path=self._db_path,
                    ), name=f"persist-failed-{trade.id[:8]}")
                    await self._mark_skipped(signal, "LIVE_NOT_WIRED")
                    return
                except PolymarketAPIError as exc:
                    tripped = self._risk.record_post_fail()
                    # Atualiza trade em RAM e dispara persist com status="failed".
                    trade.status = "failed"
                    trade.error = str(exc)[:500]
                    asyncio.create_task(persist_trade_async(
                        trade, conn=self._conn, db_path=self._db_path,
                    ), name=f"persist-failed-{trade.id[:8]}")
                    if tripped and self._on_event:
                        await self._on_event("risk_halt", signal, None)
                    await self._mark_skipped(signal, f"POST_FAIL: {exc}")
                    return
        elif self._cfg.mode == "dry-run":
            log.info("dry_run_skip_post", trade_id=trade.id)
            # Dry-run ainda persiste trade (auditoria) — fire-and-forget.
            asyncio.create_task(persist_trade_async(
                trade, conn=self._conn, db_path=self._db_path,
            ), name=f"persist-dryrun-{trade.id[:8]}")
            return

        # 3. SUCESSO: persist em paralelo + apply_fill.
        # `create_task` retorna imediatamente; o INSERT não atrasa apply_fill.
        asyncio.create_task(persist_trade_async(
            trade, conn=self._conn, db_path=self._db_path,
        ), name=f"persist-{trade.id[:8]}")

        # 4. Update positions — write-through state cache.
        await apply_fill(
            signal=signal, trade=trade,
            executed_size=executed_size, executed_price=executed_price,
            db_path=self._db_path,
            state=self._state,
            conn=self._conn,
        )

        if self._conn is not None:
            await self._conn.execute(
                "UPDATE trade_signals SET status='executed' WHERE id=?",
                (signal.id,),
            )
            await self._conn.commit()
        else:
            async with get_connection(self._db_path) as db:
                await db.execute(
                    "UPDATE trade_signals SET status='executed' WHERE id=?",
                    (signal.id,),
                )
                await db.commit()

        metrics.trades_executed.inc()
        metrics.signal_to_fill_seconds.observe(_t.perf_counter() - start_perf)
        if self._on_event:
            await self._on_event("executed", signal, trade)

    async def _process_one(self, signal: TradeSignal) -> None:
        """Wrapper de tarefa por sinal — captura crash e libera task_done."""
        try:
            await self.handle_signal(signal)
        except Exception:  # noqa: BLE001 — crash iso por sinal
            metrics.errors.labels(source="executor").inc()
            log.exception("copy_engine_crash", signal_id=signal.id)
        finally:
            self._queue.task_done()

    async def run_loop(self) -> None:
        """Consumidor concorrente da fila de signals.

        Cria uma task por sinal — paralelismo real até o teto do
        Semaphore (`max_concurrent_signals`). O lock interno
        (`_risk_lock`) garante que decisão+sizing sejam atômicas, evitando
        over-subscription de banca em rajadas.
        """
        while True:
            signal = await self._queue.get()
            asyncio.create_task(
                self._process_one(signal),
                name=f"copy-signal-{signal.id[:8]}",
            )
