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
from src.executor.order_manager import build_draft
from src.executor.order_watchdog import watchdog_order
from src.executor.position_manager import apply_fill
from src.executor.risk_manager import RiskManager
from src.executor.slippage import check_slippage_or_abort

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

    async def _mark_trade_failed(self, trade_id: str, error: str) -> None:
        """C2 — Atualiza copy_trades.status='failed' quando post_order rejeita."""
        if self._conn is not None:
            await self._conn.execute(
                "UPDATE copy_trades SET status='failed', error=? WHERE id=?",
                (error[:500], trade_id),
            )
            await self._conn.commit()
        else:
            async with get_connection(self._db_path) as db:
                await db.execute(
                    "UPDATE copy_trades SET status='failed', error=? WHERE id=?",
                    (error[:500], trade_id),
                )
                await db.commit()

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

        if perfect_mirror:
            # Realista: sizing respeita cash DISPONÍVEL (banca - capital
            # travado em posições abertas + realized PnL). Sem isso, o
            # paper simula investir $32k com banca de $50 — distorce toda
            # análise de viabilidade do bot em live.
            # SELL não consome caixa (é saída de token); permite sempre.
            starting_bank = float(self._cfg.max_portfolio_usd)
            cash_available = starting_bank
            if signal.side == "BUY" and self._conn is not None:
                async with self._conn.execute(
                    "SELECT COALESCE(SUM(size*avg_entry_price),0), "
                    "       COALESCE(SUM(realized_pnl),0) "
                    "FROM bot_positions"
                ) as cur:
                    row = await cur.fetchone()
                invested_open = float(row[0]) if row else 0.0
                realized = float(row[1]) if row else 0.0
                cash_available = starting_bank + realized - invested_open
                # Mínimo Polymarket: $1. Se cash < $1, skip com razão clara.
                if cash_available < 1.0:
                    await self._mark_skipped(
                        signal,
                        f"INSUFFICIENT_CASH: ${cash_available:.2f} restante de ${starting_bank:.0f}",
                    )
                    return
            target = starting_bank * self._cfg.proportional_factor
            sized = min(target, self._cfg.max_position_usd, cash_available)
            sized = max(sized, 1.0)  # Polymarket mín $1
            class _D:
                allowed = True
                reason = "OK"
                sized_usd = sized
            decision = _D()
            ref_price = signal.price  # bypass slippage anchor — usa preço whale
        else:
            # 1. Risk gates
            decision = self._risk.evaluate(signal, self._risk_state())
            if not decision.allowed:
                await self._mark_skipped(signal, f"RISK: {decision.reason}")
                return
            # 2. Regra 1 + Spread shield
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
                return
            except SlippageExceededError as exc:
                await self._mark_skipped(signal, f"SLIPPAGE: {exc.actual:.4f}")
                return
            except PolymarketAPIError as exc:
                await self._mark_skipped(signal, f"BOOK_FETCH: {exc}")
                return

        # 2.5. H6 — Tag exposure cap (só para BUY; SELL reduz exposure).
        if not perfect_mirror and signal.side == "BUY" and self._conn is not None:
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
                    return
            except Exception as exc:  # noqa: BLE001 — soft-fail: não bloquear trade
                log.warning("tag_exposure_check_failed", err=repr(exc))

        # 3. Build order (quantiza + neg_risk)
        draft, trade, _spec = await build_draft(
            signal=signal, sized_usd=decision.sized_usd, ref_price=ref_price,
            gamma=self._gamma, cfg=self._cfg, db_path=self._db_path,
        )

        # 4. Submit
        executed_price = float(draft.price)
        executed_size = float(draft.size)
        if self._cfg.mode == "live":
            try:
                post_resp = await self._clob.post_order(
                    draft, order_type=self._cfg.default_order_type,
                )
                self._risk.record_post_success()
                # H3 — FOK watchdog fire-and-forget (não bloqueia pipeline).
                if self._cfg.fok_fallback and post_resp:
                    order_id = post_resp.get("orderID") or post_resp.get("orderId") or ""
                    if order_id:
                        asyncio.create_task(watchdog_order(
                            clob=self._clob, order_id=order_id, draft=draft,
                            timeout_s=self._cfg.fok_fallback_timeout_seconds,
                        ), name=f"watchdog-{order_id[:8]}")
            except NotImplementedError:
                log.error("live_mode_not_wired", signal_id=signal.id)
                await self._mark_skipped(signal, "LIVE_NOT_WIRED")
                return
            except PolymarketAPIError as exc:
                # Circuit breaker — N falhas consecutivas → halt global.
                tripped = self._risk.record_post_fail()
                skip_reason = f"POST_FAIL: {exc}"
                # C2 — marca copy_trades como failed pra evitar orphan pending.
                await self._mark_trade_failed(trade.id, str(exc))
                if tripped and self._on_event:
                    await self._on_event("risk_halt", signal, None)
                await self._mark_skipped(signal, skip_reason)
                return
        elif self._cfg.mode == "dry-run":
            log.info("dry_run_skip_post", trade_id=trade.id)
            return

        # 5. Update positions — state cache write-through evita que o
        # próximo SELL bloqueie por "exit_sync_no_position".
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
        metrics.signal_to_fill_seconds.observe(_t.perf_counter() - _start)
        if self._on_event:
            await self._on_event("executed", signal, trade)

    async def run_loop(self) -> None:
        while True:
            signal = await self._queue.get()
            try:
                await self.handle_signal(signal)
            except Exception:  # noqa: BLE001 — loop resiliente; crash iso por sinal
                metrics.errors.labels(source="executor").inc()
                log.exception("copy_engine_crash", signal_id=signal.id)
            finally:
                self._queue.task_done()
