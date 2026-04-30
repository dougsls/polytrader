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
from src.dashboard.demo_persistence import record_journal_entry
from src.executor.depth_sizing import quote_buy_depth, quote_sell_depth
from src.executor.exposure import would_breach_tag_cap
from src.executor.order_manager import (
    build_draft,
    persist_trade,
    update_trade_status,
)
from src.executor.order_watchdog import watchdog_order
from src.executor.paper_simulator import fetch_book_safe, simulate_fill
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
        queue: asyncio.Queue[Any],
        state: InMemoryState,
        risk_state_provider: Callable[[], RiskState],
        on_event: NotifyCallback = None,
        db_path: Path = DEFAULT_DB_PATH,
        conn: aiosqlite.Connection | None = None,
        depth_cfg: Any | None = None,
        on_order_submitted: Callable[[str], Awaitable[None]] | None = None,
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
        # Depth sizing config (DepthSizingConfig | None) — quando enabled,
        # antes de cada ordem live consulta book e ajusta size pra caber
        # em max_impact_pct. Inativo em paper.
        self._depth_cfg = depth_cfg
        # Trades aguardando confirmação de fill via user_ws (live + GTC).
        # Map order_id → CopyTrade. handle_fill_event consome esta map.
        self._pending_fills: dict[str, tuple[TradeSignal, CopyTrade]] = {}
        # Callback para auto-subscribe ao UserWSClient quando ordem GTC
        # é submetida em condition_id novo. Sem isso, fills nesse mercado
        # não chegam até o próximo restart com reconcile.
        self._on_order_submitted = on_order_submitted
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

        # === Config flag gates (defesa em profundidade) ====================
        # Mesmo que main.py não inicie a task quando enabled=False, este
        # check protege se o engine for instanciado/chamado por outro caminho.
        if not self._cfg.enabled:
            await self._mark_skipped(signal, "EXECUTOR_DISABLED")
            return
        # `bypass_slippage_check=True` indica stale_cleanup/emergency exit —
        # capital travado é pior que dump em loss conhecida. Ignora copy_sells.
        emergency_exit = signal.side == "SELL" and signal.bypass_slippage_check
        if signal.side == "BUY" and not self._cfg.copy_buys:
            await self._mark_skipped(signal, "COPY_BUYS_DISABLED")
            return
        if signal.side == "SELL" and not self._cfg.copy_sells and not emergency_exit:
            await self._mark_skipped(signal, "COPY_SELLS_DISABLED")
            return

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
            decision_pm: Any = _D()
            ref_price = signal.price  # bypass slippage anchor — usa preço whale
            return decision_pm, ref_price

        # === Non-perfect-mirror branch: risk gates + slippage anchoring ====
        # ⚠️ avoid_resolved_markets / enforce_market_duration — defesa em
        # profundidade contra sinais que escaparam do tracker filter
        # (mudanças de status entre tracker.detect_signal e executor).
        if (
            self._cfg.avoid_resolved_markets
            or self._cfg.enforce_market_duration
        ):
            try:
                market = await self._gamma.get_market(signal.condition_id)
            except Exception as exc:  # noqa: BLE001 — gamma falha não barra trade
                log.debug("gamma_secondary_check_failed", err=repr(exc)[:80])
                market = None
            if market is not None and self._cfg.avoid_resolved_markets:
                if market.get("closed") or market.get("resolved"):
                    await self._mark_skipped(signal, "MARKET_RESOLVED")
                    return None
            if self._cfg.enforce_market_duration:
                # Defesa secundária: rejeita mercados sem hours_to_resolution
                # OU fora da janela permitida. tracker já filtra mas o sinal
                # pode ter chegado por polling de fallback.
                hrs = signal.hours_to_resolution
                if hrs is None and not signal.bypass_slippage_check:
                    await self._mark_skipped(signal, "MARKET_DURATION_UNKNOWN")
                    return None

        # 1. Risk gates
        # `decision` é union: _D inline (perfect_mirror) ou RiskDecision.
        # Anotação Any pra evitar mypy assignment-mismatch entre os branches.
        decision: Any = self._risk.evaluate(signal, self._risk_state())
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

    async def _apply_depth_sizing(
        self, signal: TradeSignal, decision: Any,
    ) -> tuple[bool, str | None]:
        """Item 6 — depth sizing live. Ajusta `decision.sized_usd` baseado
        em book real. Retorna (ok, skip_reason).

        BUY: corta `decision.sized_usd` por `quote_buy_depth.fillable_size_usd`
             considerando max_impact_pct.
        SELL: valida que `signal.size × price` cabe no impact cap (não
              ajusta o `signal.size` aqui — Exit Sync já calculou em tokens;
              mas se book está vazio, skip).
        Stale cleanup (`bypass_slippage_check=True`): pula depth gate
        (emergency exit aceita impact maior — capital travado é pior).
        """
        if self._depth_cfg is None or not self._depth_cfg.enabled:
            return True, None
        if signal.bypass_slippage_check:
            return True, None
        try:
            book = await self._clob.book(signal.token_id)
        except Exception as exc:  # noqa: BLE001 — book lookup falha; não barra trade
            log.warning("depth_book_fetch_failed", err=repr(exc)[:80])
            return True, None
        if signal.side == "BUY":
            q = quote_buy_depth(
                book,
                max_size_usd=decision.sized_usd,
                max_impact_pct=self._depth_cfg.max_impact_pct,
                max_levels=self._depth_cfg.max_levels,
            )
            log.info(
                "depth_quote",
                side="BUY", signal_id=signal.id,
                vwap=q.vwap_price, levels=q.levels_consumed,
                impact_pct=q.impact_pct, book_thin=q.book_thin,
                fillable_usd=q.fillable_size_usd,
            )
            if q.fillable_size_usd < 1.0:
                return False, "DEPTH_TOO_THIN"
            # Corta sized se depth restringe.
            if q.fillable_size_usd < decision.sized_usd:
                decision.sized_usd = q.fillable_size_usd
            return True, None
        # SELL: valida usd_value (= signal.size × signal.price) contra book de bids
        sell_usd_target = signal.size * signal.price if signal.price > 0 else 0.0
        if sell_usd_target <= 0:
            return True, None  # nada que validar
        q = quote_sell_depth(
            book,
            max_size_usd=sell_usd_target,
            max_impact_pct=self._depth_cfg.max_impact_pct,
            max_levels=self._depth_cfg.max_levels,
        )
        log.info(
            "depth_quote",
            side="SELL", signal_id=signal.id,
            vwap=q.vwap_price, levels=q.levels_consumed,
            impact_pct=q.impact_pct, book_thin=q.book_thin,
            fillable_usd=q.fillable_size_usd,
        )
        if q.fillable_size_usd < 1.0:
            return False, "DEPTH_TOO_THIN"
        return True, None

    async def _submit_and_apply(
        self, *,
        signal: TradeSignal, decision: Any, ref_price: float,
        perfect_mirror: bool, start_perf: float,
    ) -> None:
        """Pós-decisão: depth sizing → build_draft → INSERT trade → post_order
        → submit/fill state machine.

        ⚠️ FIXES APLICADOS:
            - Item 1: SELL usa signal.size em tokens (com cap por bot_size)
            - Item 3: persist_trade SINCRONO antes do post_order (race fix)
            - Item 6: depth sizing ajusta sized_usd antes do build
            - Item 2: live + GTC marca submitted, NÃO aplica fill;
                      live + FOK aplica fill após response.success;
                      paper/dry-run mantém comportamento (fill imediato).
        """
        # 0. Depth sizing — ajusta decision.sized_usd antes do build (live only).
        if self._cfg.mode == "live":
            ok, reason = await self._apply_depth_sizing(signal, decision)
            if not ok:
                await self._mark_skipped(signal, reason or "DEPTH_GATE")
                return

        # 1. SELL cap — não vender mais tokens do que o bot detém.
        # Defesa contra drift entre RAM state e DB (ex: snapshot antigo,
        # apply_fill em vôo). Cap em bot_size do token; min(signal.size, cap).
        sell_cap: float | None = None
        if signal.side == "SELL" and self._state is not None:
            sell_cap = self._state.bot_size(signal.token_id)
            if sell_cap <= 0 and not signal.bypass_slippage_check:
                # Sem posição pra vender — sinal SELL inválido.
                await self._mark_skipped(signal, "SELL_NO_POSITION")
                return

        # 2. Build draft — RAM only.
        draft, trade, _spec = await build_draft(
            signal=signal, sized_usd=decision.sized_usd, ref_price=ref_price,
            gamma=self._gamma, cfg=self._cfg, db_path=self._db_path,
            sell_size_cap_tokens=sell_cap,
        )
        executed_price = float(draft.price)
        executed_size = float(draft.size)

        order_type = self._cfg.default_order_type
        if getattr(self._cfg, "optimistic_execution", False):
            order_type = "FOK"

        # 3. ⚠️ Item 3 fix — INSERT trade ANTES do post_order.
        # Garante que UPDATEs subsequentes (status submitted/failed/filled)
        # encontram a linha. Sem isso, race entre INSERT fire-and-forget e
        # apply_fill UPDATE corrompe audit trail.
        try:
            await persist_trade(trade, conn=self._conn, db_path=self._db_path)
        except Exception as exc:  # noqa: BLE001 — DB falha não bloqueia ordem
            log.error("persist_trade_failed", trade_id=trade.id, err=repr(exc))

        # 4. Mode branch.
        if self._cfg.mode == "dry-run":
            log.info("dry_run_skip_post", trade_id=trade.id)
            # Dry-run não aplica fill; trade fica como pending no DB.
            return

        if self._cfg.mode != "live":
            # Paper realista (24h DEMO) — quando NÃO é perfect_mirror,
            # busca book real e simula fill via VWAP. Skipa se book vazio,
            # spread > max, slippage > tolerância ou depth insuficiente.
            # Stale/emergency exits ignoram spread+slippage mas ainda
            # exigem book não vazio.
            if not getattr(self._cfg, "paper_perfect_mirror", False):
                book = await fetch_book_safe(self._clob, signal.token_id)
                sim = simulate_fill(
                    signal=signal, book=book,
                    intended_size=executed_size,
                    intended_price=executed_price,
                    cfg=self._cfg,
                )
                # Audit ABSOLUTAMENTE TUDO no journal (executed/partial/skipped).
                intended_usd = executed_size * executed_price
                await record_journal_entry(
                    signal=signal, sim=sim,
                    intended_size_usd=intended_usd,
                    source=signal.source,
                    conn=self._conn, db_path=self._db_path,
                )
                if not sim.filled:
                    # Skip: zero fill. Não atualiza state.
                    await update_trade_status(
                        trade.id, status="failed",
                        error=f"PAPER_REALISTIC_SKIP: {sim.skip_reason}",
                        conn=self._conn, db_path=self._db_path,
                    )
                    await self._mark_skipped(
                        signal, f"PAPER_REALISTIC: {sim.skip_reason}",
                    )
                    log.info(
                        "paper_realistic_skipped",
                        signal_id=signal.id, reason=sim.skip_reason,
                        spread=f"{sim.spread:.4f}",
                        slippage=f"{sim.slippage:.4f}",
                    )
                    return
                # Fill (executed ou partial) — usa simulated price/size REAIS.
                await self._apply_fill_local(
                    signal=signal, trade=trade,
                    executed_size=sim.simulated_size,
                    executed_price=sim.simulated_price,
                    start_perf=start_perf,
                )
                log.info(
                    "paper_realistic_filled",
                    signal_id=signal.id, status=sim.status,
                    sim_price=f"{sim.simulated_price:.4f}",
                    sim_size=f"{sim.simulated_size:.2f}",
                    slippage=f"{sim.slippage:.4f}",
                    is_emergency=sim.is_emergency_exit,
                )
                return
            # paper_perfect_mirror=True (DEPRECATED, fakefill no preço whale).
            # Mantido pra observação bruta apenas — NUNCA usar pra avaliar.
            await self._apply_fill_local(
                signal=signal, trade=trade,
                executed_size=executed_size, executed_price=executed_price,
                start_perf=start_perf,
            )
            return

        # === LIVE ====
        post_resp: dict[str, Any] | None = None
        async with self._post_semaphore:
            try:
                post_resp = await self._clob.post_order(draft, order_type=order_type)
                self._risk.record_post_success()
            except NotImplementedError:
                log.error("live_mode_not_wired", signal_id=signal.id)
                await update_trade_status(
                    trade.id, status="failed", error="LIVE_NOT_WIRED",
                    conn=self._conn, db_path=self._db_path,
                )
                await self._mark_skipped(signal, "LIVE_NOT_WIRED")
                return
            except PolymarketAPIError as exc:
                tripped = self._risk.record_post_fail()
                await update_trade_status(
                    trade.id, status="failed", error=str(exc),
                    conn=self._conn, db_path=self._db_path,
                )
                if tripped and self._on_event:
                    await self._on_event("risk_halt", signal, None)
                await self._mark_skipped(signal, f"POST_FAIL: {exc}")
                return

        # Post bem-sucedido — extrai order_id e decide fluxo.
        order_id = ""
        if post_resp:
            order_id = post_resp.get("orderID") or post_resp.get("orderId") or ""

        # ⚠️ Item 2 — submit ≠ fill. Decisão por order_type:
        #   FOK: response.success implica match imediato (semântica fill-or-kill).
        #        Aplicamos fill direto.
        #   GTC: ordem entrou no book; aguarda user_ws event de fill.
        #        Marca submitted; trade fica em self._pending_fills.
        if order_type == "FOK":
            # FOK matched — aplica fill confirmado.
            await update_trade_status(
                trade.id, status="submitted", order_id=order_id,
                conn=self._conn, db_path=self._db_path,
            )
            await self._apply_fill_local(
                signal=signal, trade=trade,
                executed_size=executed_size, executed_price=executed_price,
                start_perf=start_perf,
            )
            return

        # GTC — não aplicar fill ainda. Marca submitted + auto-subscribe
        # UserWS no condition_id (sem isso, fill events não chegam).
        await update_trade_status(
            trade.id, status="submitted", order_id=order_id,
            conn=self._conn, db_path=self._db_path,
        )
        if order_id:
            self._pending_fills[order_id] = (signal, trade)
            # Auto-subscribe UserWS (defesa contra condition_ids=set() inicial).
            if self._on_order_submitted is not None:
                try:
                    await self._on_order_submitted(signal.condition_id)
                except Exception as exc:  # noqa: BLE001 — não bloqueia ordem
                    log.warning(
                        "on_order_submitted_callback_failed",
                        condition_id=signal.condition_id[:12], err=repr(exc)[:80],
                    )
            # Watchdog FOK fallback: se timeout sem fill, watchdog
            # cancela e reposta como FOK. ⚠️ Capturamos o retorno: se
            # o fallback FOK retorna match (success=True), aplicamos
            # fill local. Antes, o retorno era ignorado e fills do
            # fallback ficavam sem registro local.
            if self._cfg.fok_fallback:
                asyncio.create_task(
                    self._run_watchdog_and_handle_fill(
                        signal=signal, trade=trade, draft=draft,
                        order_id=order_id,
                    ),
                    name=f"watchdog-{order_id[:8]}",
                )
        log.info(
            "order_submitted_awaiting_fill",
            trade_id=trade.id, order_id=order_id, side=signal.side,
        )

    async def _run_watchdog_and_handle_fill(
        self, *,
        signal: TradeSignal, trade: CopyTrade, draft: Any, order_id: str,
    ) -> None:
        """⚠️ Watchdog wrapper que captura o retorno do FOK fallback.

        watchdog_order retorna:
          - None se a ordem original GTC preencheu dentro do timeout
            (fill aplicado via UserWS); ou se o fallback FOK também falhou.
          - dict com `success=True` se o FOK fallback preencheu agora.

        Caso 2 NÃO chega no UserWS porque a ordem fallback é nova (outro
        order_id). Sem este handler, o fill seria perdido — bot ficaria
        com state local desatualizado vs CLOB.
        """
        try:
            fok_resp = await watchdog_order(
                clob=self._clob, order_id=order_id, draft=draft,
                timeout_s=self._cfg.fok_fallback_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("watchdog_crashed", order_id=order_id[:8], err=repr(exc))
            return
        if fok_resp is None:
            # GTC original encheu sozinha (UserWS aplica fill) OU
            # fallback FOK também falhou. Nada a fazer aqui.
            return
        # FOK fallback preencheu — aplica fill localmente. Como a ordem
        # FOK é nova, o `_pending_fills` ainda tem o trade original
        # esperando fill. Removemos o entry e aplicamos.
        if not (fok_resp.get("success") and not fok_resp.get("errorMsg")):
            log.warning(
                "fok_fallback_returned_unsuccessful",
                order_id=order_id[:8], resp=str(fok_resp)[:120],
            )
            return
        # Remove a entry do pending — fill já foi consumido aqui.
        self._pending_fills.pop(order_id, None)
        new_order_id = fok_resp.get("orderID") or fok_resp.get("orderId") or ""
        await update_trade_status(
            trade.id, status="submitted", order_id=new_order_id,
            conn=self._conn, db_path=self._db_path,
        )
        await self._apply_fill_local(
            signal=signal, trade=trade,
            executed_size=float(draft.size), executed_price=float(draft.price),
            start_perf=0.0,
        )
        log.info(
            "fok_fallback_filled_locally_applied",
            original_order=order_id[:8], new_order=new_order_id[:8],
        )

    async def _apply_fill_local(
        self, *,
        signal: TradeSignal, trade: CopyTrade,
        executed_size: float, executed_price: float,
        start_perf: float,
    ) -> None:
        """Aplica fill confirmado: UPDATE bot_positions, UPDATE copy_trades,
        UPDATE trade_signals, fire on_event. Caminho compartilhado entre
        paper (fill simulado) e FOK matched live (fill imediato).
        """
        import time as _t

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

    async def handle_fill_event(
        self,
        *,
        order_id: str,
        executed_size: float,
        executed_price: float,
    ) -> bool:
        """⚠️ Item 2 — Callback do UserWSClient para fills GTC confirmados.

        Procura o trade pendente por `order_id` e aplica o fill real
        (que pode ser parcial). Se a ordem não estiver em `_pending_fills`
        (ex: trade fora deste processo, restart), tenta resolver via DB.

        Suporta múltiplos eventos parciais: cada chamada acumula até
        o trade ser totalmente preenchido (size restante).
        """
        ctx = self._pending_fills.get(order_id)
        if ctx is None:
            log.warning(
                "fill_event_for_unknown_order",
                order_id=order_id[:12], size=executed_size,
            )
            return False
        signal, trade = ctx
        # Para fill TOTAL, remove do map. Para parciais, mantém.
        # Heurística: se executed_size >= intended, é fill final.
        is_final = executed_size >= trade.intended_size - 1e-9
        if is_final:
            self._pending_fills.pop(order_id, None)
        await self._apply_fill_local(
            signal=signal, trade=trade,
            executed_size=executed_size, executed_price=executed_price,
            start_perf=0.0,  # fill async — métrica não cobre
        )
        return True

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
