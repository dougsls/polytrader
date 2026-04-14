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
from src.core.models import CopyTrade, RiskState, TradeSignal
from src.core.state import InMemoryState
from src.executor.order_manager import build_draft
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

    async def _mark_skipped(self, signal: TradeSignal, reason: str) -> None:
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
        # 1. Risk gates
        decision = self._risk.evaluate(signal, self._risk_state())
        if not decision.allowed:
            await self._mark_skipped(signal, f"RISK: {decision.reason}")
            return

        # 2. Regra 1 + Spread shield (dupla trava pré-submissão)
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
                await self._clob.post_order(draft, order_type=self._cfg.default_order_type)
                self._risk.record_post_success()
            except NotImplementedError:
                log.error("live_mode_not_wired", signal_id=signal.id)
                await self._mark_skipped(signal, "LIVE_NOT_WIRED")
                return
            except PolymarketAPIError as exc:
                # Circuit breaker — N falhas consecutivas → halt global.
                tripped = self._risk.record_post_fail()
                skip_reason = f"POST_FAIL: {exc}"
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

        if self._on_event:
            await self._on_event("executed", signal, trade)

    async def run_loop(self) -> None:
        while True:
            signal = await self._queue.get()
            try:
                await self.handle_signal(signal)
            except Exception:  # noqa: BLE001 — loop resiliente; crash iso por sinal
                log.exception("copy_engine_crash", signal_id=signal.id)
            finally:
                self._queue.task_done()
