"""Live order lifecycle tracking driven by exchange truth.

Responsibilities:
    - persist submitted orders and their lifecycle states;
    - persist confirmed fill events idempotently;
    - apply position changes only from confirmed fills;
    - recover open submitted/partial orders after restart.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import orjson

from src.core.database import DEFAULT_DB_PATH, get_connection
from src.core.logger import get_logger
from src.core.models import CopyTrade, TradeSignal
from src.core.state import InMemoryState
from src.executor.position_manager import apply_fill

log = get_logger(__name__)

_EPS = 1e-9
_TERMINAL_STATUSES = {"filled", "cancelled", "canceled", "rejected"}
_OPEN_STATUSES = {"pending", "live", "open", "unmatched", "submitted"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    if isinstance(value, str) and value:
        return value
    return _utc_now()


def _extract_order_id(payload: dict[str, Any]) -> str:
    direct = payload.get("order_id") or payload.get("orderID") or payload.get("id")
    if isinstance(direct, str) and direct:
        return direct
    for key in ("maker_orders", "makerOrders", "taker_orders", "takerOrders"):
        orders = payload.get(key) or []
        if isinstance(orders, list) and orders:
            first = orders[0]
            if isinstance(first, str) and first:
                return first
            if isinstance(first, dict):
                candidate = (
                    first.get("order_id")
                    or first.get("orderID")
                    or first.get("id")
                )
                if isinstance(candidate, str) and candidate:
                    return candidate
    return ""


def _extract_fill_id(payload: dict[str, Any], order_id: str) -> str:
    fill_id = payload.get("trade_id") or payload.get("tradeID") or payload.get("match_id")
    fill_id = fill_id or payload.get("fill_id") or payload.get("id")
    if isinstance(fill_id, str) and fill_id:
        return fill_id
    timestamp = _as_iso(payload.get("timestamp") or payload.get("filled_at"))
    size = float(payload.get("size_matched") or payload.get("matched_size") or 0.0)
    price = float(payload.get("price") or payload.get("avg_price") or 0.0)
    return f"{order_id}:{timestamp}:{size:.8f}:{price:.8f}"


def _extract_status(payload: dict[str, Any], intended_size: float, matched_size: float) -> str:
    raw = str(payload.get("status") or payload.get("order_status") or "submitted").lower()
    if raw == "canceled":
        return "cancelled"
    if raw == "rejected":
        return "rejected"
    if matched_size >= max(intended_size - _EPS, 0.0):
        return "filled"
    if raw in _TERMINAL_STATUSES:
        return "cancelled" if raw == "canceled" else raw
    if matched_size > _EPS:
        return "partial"
    if raw in _OPEN_STATUSES:
        return "submitted"
    return "submitted"


class LiveOrderTracker:
    def __init__(
        self,
        *,
        state: InMemoryState,
        conn: aiosqlite.Connection | None = None,
        db_path: Path = DEFAULT_DB_PATH,
        condition_ids: set[str] | None = None,
        on_market_subscription_change: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._state = state
        self._conn = conn
        self._db_path = db_path
        self._condition_ids = condition_ids if condition_ids is not None else set()
        self._on_market_subscription_change = on_market_subscription_change

    def set_subscription_callback(
        self,
        callback: Callable[[], Awaitable[None]] | None,
    ) -> None:
        self._on_market_subscription_change = callback

    async def _track_condition(self, condition_id: str) -> None:
        if not condition_id or condition_id in self._condition_ids:
            return
        self._condition_ids.add(condition_id)
        if self._on_market_subscription_change is not None:
            await self._on_market_subscription_change()

    async def record_submitted_order(
        self,
        *,
        trade_id: str,
        signal_id: str,
        condition_id: str,
        order_id: str,
        submitted_at: str | None = None,
    ) -> None:
        now = submitted_at or _utc_now()

        async def _apply(db: aiosqlite.Connection) -> None:
            await db.execute(
                """
                UPDATE copy_trades
                SET order_id=?, status='submitted',
                    submitted_at=COALESCE(submitted_at, ?), updated_at=?
                WHERE id=?
                """,
                (order_id, now, now, trade_id),
            )
            await db.execute(
                "UPDATE trade_signals SET status='executing' WHERE id=?",
                (signal_id,),
            )
            await db.execute(
                """
                INSERT INTO copy_trade_orders
                    (copy_trade_id, order_id, condition_id, status, submitted_at, last_update_at)
                VALUES (?, ?, ?, 'submitted', ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    status='submitted',
                    last_update_at=excluded.last_update_at
                """,
                (trade_id, order_id, condition_id, now, now),
            )
            await db.commit()

        if self._conn is not None:
            await _apply(self._conn)
        else:
            async with get_connection(self._db_path) as db:
                await _apply(db)
        await self._track_condition(condition_id)

    async def replace_active_order(
        self,
        *,
        trade_id: str,
        condition_id: str,
        order_id: str,
        submitted_at: str | None = None,
    ) -> None:
        now = submitted_at or _utc_now()

        async def _apply(db: aiosqlite.Connection) -> None:
            async with db.execute(
                "SELECT COALESCE(executed_size, 0) FROM copy_trades WHERE id=?",
                (trade_id,),
            ) as cur:
                row = await cur.fetchone()
            executed_size = float(row[0]) if row is not None else 0.0
            status = "partial" if executed_size > _EPS else "submitted"
            await db.execute(
                "UPDATE copy_trades SET order_id=?, status=?, updated_at=? WHERE id=?",
                (order_id, status, now, trade_id),
            )
            await db.execute(
                """
                INSERT INTO copy_trade_orders
                    (copy_trade_id, order_id, condition_id, status, submitted_at, last_update_at)
                VALUES (?, ?, ?, 'submitted', ?, ?)
                """,
                (trade_id, order_id, condition_id, now, now),
            )
            await db.commit()

        if self._conn is not None:
            await _apply(self._conn)
        else:
            async with get_connection(self._db_path) as db:
                await _apply(db)
        await self._track_condition(condition_id)

    async def handle_user_message(self, payload: dict[str, Any]) -> None:
        event_type = str(payload.get("event_type") or payload.get("type") or "").lower()
        if event_type == "trade":
            await self._handle_trade_event(payload)
        elif event_type == "order":
            await self._handle_order_event(payload)

    async def reconcile_open_orders(self, *, clob: Any) -> None:
        async def _load_open_orders(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
            async with db.execute(
                """
                SELECT order_id, condition_id
                FROM copy_trade_orders
                WHERE status IN ('submitted', 'partial')
                """
            ) as cur:
                return await cur.fetchall()

        if self._conn is not None:
            rows = await _load_open_orders(self._conn)
        else:
            async with get_connection(self._db_path) as db:
                rows = await _load_open_orders(db)

        for row in rows:
            await self._track_condition(str(row["condition_id"]))
            try:
                snapshot = await clob.get_order(str(row["order_id"]))
            except Exception as exc:  # noqa: BLE001
                log.warning("open_order_reconcile_failed", order_id=row["order_id"], err=repr(exc))
                continue
            if isinstance(snapshot, dict):
                await self._apply_order_snapshot(snapshot, fallback_order_id=str(row["order_id"]))

    async def _handle_trade_event(self, payload: dict[str, Any]) -> None:
        order_id = _extract_order_id(payload)
        fill_id = _extract_fill_id(payload, order_id)
        size = float(payload.get("size_matched") or payload.get("matched_size") or 0.0)
        price = float(payload.get("price") or payload.get("avg_price") or 0.0)
        if not order_id or size <= _EPS or price <= 0:
            log.warning("user_trade_event_ignored", payload=payload)
            return
        await self._record_fill(
            order_id=order_id,
            fill_id=fill_id,
            fill_size=size,
            fill_price=price,
            filled_at=_as_iso(payload.get("timestamp") or payload.get("filled_at")),
            raw_event=payload,
        )

    async def _handle_order_event(self, payload: dict[str, Any]) -> None:
        await self._apply_order_snapshot(payload, fallback_order_id=_extract_order_id(payload))

    async def _apply_order_snapshot(
        self,
        payload: dict[str, Any],
        *,
        fallback_order_id: str,
    ) -> None:
        order_id = _extract_order_id(payload) or fallback_order_id
        if not order_id:
            return

        async def _apply(db: aiosqlite.Connection) -> None:
            order_ctx = await self._load_order_context(db, order_id)
            if order_ctx is None:
                log.warning("order_snapshot_unknown_order", order_id=order_id)
                return

            matched_size = float(
                payload.get("size_matched")
                or payload.get("matched_size")
                or payload.get("filled_size")
                or payload.get("filledSize")
                or 0.0
            )
            avg_price = float(
                payload.get("avg_price")
                or payload.get("average_price")
                or payload.get("averagePrice")
                or payload.get("price")
                or 0.0
            )
            status = _extract_status(payload, order_ctx["intended_size"], matched_size)
            event_at = _as_iso(payload.get("timestamp") or payload.get("updated_at"))

            current_size, current_notional = await self._fill_totals(db, order_id=order_id)
            if matched_size > current_size + _EPS and avg_price > 0:
                target_notional = matched_size * avg_price
                delta_size = matched_size - current_size
                delta_notional = max(target_notional - current_notional, 0.0)
                delta_price = avg_price if delta_size <= _EPS else delta_notional / delta_size
                await self._record_fill(
                    order_id=order_id,
                    fill_id=f"reconcile:{matched_size:.8f}",
                    fill_size=delta_size,
                    fill_price=delta_price,
                    filled_at=event_at,
                    raw_event=payload,
                    db=db,
                )

            await db.execute(
                """
                UPDATE copy_trade_orders
                SET status=?, matched_size=?, avg_price=?, error=?, raw_event_json=?, last_update_at=?
                WHERE order_id=?
                """,
                (
                    status,
                    matched_size,
                    avg_price if avg_price > 0 else None,
                    str(payload.get("error") or payload.get("errorMsg") or "") or None,
                    orjson.dumps(payload).decode(),
                    event_at,
                    order_id,
                ),
            )
            await self._update_trade_status(
                db,
                copy_trade_id=str(order_ctx["copy_trade_id"]),
                signal_id=str(order_ctx["signal_id"]),
                status=status,
                event_at=event_at,
                error=str(payload.get("error") or payload.get("errorMsg") or "") or None,
            )
            await db.commit()

        if self._conn is not None:
            await _apply(self._conn)
        else:
            async with get_connection(self._db_path) as db:
                await _apply(db)

    async def _record_fill(
        self,
        *,
        order_id: str,
        fill_id: str,
        fill_size: float,
        fill_price: float,
        filled_at: str,
        raw_event: dict[str, Any],
        db: aiosqlite.Connection | None = None,
    ) -> None:
        async def _apply(conn: aiosqlite.Connection) -> None:
            order_ctx = await self._load_order_context(conn, order_id)
            if order_ctx is None:
                log.warning("fill_unknown_order", order_id=order_id)
                return
            try:
                await conn.execute(
                    """
                    INSERT INTO copy_trade_fills
                        (copy_trade_id, order_id, fill_id, size, price, filled_at, raw_event_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order_ctx["copy_trade_id"],
                        order_id,
                        fill_id,
                        fill_size,
                        fill_price,
                        filled_at,
                        orjson.dumps(raw_event).decode(),
                    ),
                )
            except aiosqlite.IntegrityError:
                log.info("duplicate_fill_event_ignored", order_id=order_id, fill_id=fill_id)
                return

            signal, trade = await self._load_signal_and_trade(
                conn,
                copy_trade_id=str(order_ctx["copy_trade_id"]),
            )
            total_size, total_notional = await self._fill_totals(
                conn,
                copy_trade_id=str(order_ctx["copy_trade_id"]),
            )
            total_price = total_notional / total_size if total_size > _EPS else fill_price
            trade_status = "filled" if total_size >= trade.intended_size - _EPS else "partial"

            await apply_fill(
                signal=signal,
                trade=trade,
                executed_size=fill_size,
                executed_price=fill_price,
                trade_executed_size=total_size,
                trade_executed_price=total_price,
                trade_status=trade_status,
                filled_at=filled_at,
                db_path=self._db_path,
                state=self._state,
                conn=conn,
            )

            order_total_size, order_total_notional = await self._fill_totals(conn, order_id=order_id)
            order_total_price = (
                order_total_notional / order_total_size
                if order_total_size > _EPS else fill_price
            )
            await conn.execute(
                """
                UPDATE copy_trade_orders
                SET matched_size=?, avg_price=?, status=?, last_update_at=?
                WHERE order_id=?
                """,
                (
                    order_total_size,
                    order_total_price,
                    "filled" if total_size >= trade.intended_size - _EPS else "partial",
                    filled_at,
                    order_id,
                ),
            )
            await self._update_trade_status(
                conn,
                copy_trade_id=trade.id,
                signal_id=trade.signal_id,
                status=trade_status,
                event_at=filled_at,
                error=None,
            )
            await conn.commit()

        if db is not None:
            await _apply(db)
        elif self._conn is not None:
            await _apply(self._conn)
        else:
            async with get_connection(self._db_path) as conn:
                await _apply(conn)

    async def _update_trade_status(
        self,
        db: aiosqlite.Connection,
        *,
        copy_trade_id: str,
        signal_id: str,
        status: str,
        event_at: str,
        error: str | None,
    ) -> None:
        async with db.execute(
            "SELECT COALESCE(executed_size, 0) FROM copy_trades WHERE id=?",
            (copy_trade_id,),
        ) as cur:
            row = await cur.fetchone()
        executed_size = float(row[0]) if row is not None else 0.0
        effective_status = status
        if status in {"cancelled", "rejected"} and executed_size > _EPS:
            effective_status = status
        elif status == "submitted" and executed_size > _EPS:
            effective_status = "partial"

        await db.execute(
            "UPDATE copy_trades SET status=?, error=COALESCE(?, error), updated_at=? WHERE id=?",
            (effective_status, error, event_at, copy_trade_id),
        )

        if effective_status == "filled":
            await db.execute(
                "UPDATE trade_signals SET status='executed' WHERE id=?",
                (signal_id,),
            )
        elif effective_status in {"cancelled", "rejected"} and executed_size <= _EPS:
            await db.execute(
                "UPDATE trade_signals SET status='failed', skip_reason=? WHERE id=?",
                (effective_status.upper(), signal_id),
            )
        else:
            await db.execute(
                "UPDATE trade_signals SET status='executing' WHERE id=?",
                (signal_id,),
            )

    async def _load_order_context(
        self,
        db: aiosqlite.Connection,
        order_id: str,
    ) -> aiosqlite.Row | None:
        async with db.execute(
            """
            SELECT o.copy_trade_id, o.condition_id, t.signal_id, t.intended_size
            FROM copy_trade_orders o
            JOIN copy_trades t ON t.id = o.copy_trade_id
            WHERE o.order_id=?
            """,
            (order_id,),
        ) as cur:
            return await cur.fetchone()

    async def _load_signal_and_trade(
        self,
        db: aiosqlite.Connection,
        *,
        copy_trade_id: str,
    ) -> tuple[TradeSignal, CopyTrade]:
        async with db.execute(
            """
            SELECT
                t.id AS trade_id,
                t.signal_id,
                t.order_id,
                t.condition_id,
                t.token_id,
                t.side AS trade_side,
                t.intended_size,
                t.executed_size,
                t.intended_price,
                t.executed_price,
                t.slippage,
                t.status AS trade_status,
                t.created_at,
                t.filled_at,
                t.error,
                s.id AS signal_row_id,
                s.wallet_address,
                s.wallet_score,
                s.condition_id AS signal_condition_id,
                s.token_id AS signal_token_id,
                s.side AS signal_side,
                s.size,
                s.price,
                s.usd_value,
                s.market_title,
                s.outcome,
                s.market_end_date,
                s.hours_to_resolution,
                s.detected_at,
                s.source,
                s.status AS signal_status,
                s.skip_reason
            FROM copy_trades t
            JOIN trade_signals s ON s.id = t.signal_id
            WHERE t.id=?
            """,
            (copy_trade_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise RuntimeError(f"copy_trade ausente: {copy_trade_id}")

        trade = CopyTrade(
            id=str(row["trade_id"]),
            signal_id=str(row["signal_id"]),
            order_id=row["order_id"],
            condition_id=str(row["condition_id"]),
            token_id=str(row["token_id"]),
            side=str(row["trade_side"]),
            intended_size=float(row["intended_size"]),
            executed_size=float(row["executed_size"]) if row["executed_size"] is not None else None,
            intended_price=float(row["intended_price"]),
            executed_price=float(row["executed_price"]) if row["executed_price"] is not None else None,
            slippage=float(row["slippage"]) if row["slippage"] is not None else None,
            status=str(row["trade_status"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            filled_at=(
                datetime.fromisoformat(str(row["filled_at"]))
                if row["filled_at"] is not None else None
            ),
            error=row["error"],
        )
        signal = TradeSignal(
            id=str(row["signal_row_id"]),
            wallet_address=str(row["wallet_address"]),
            wallet_score=float(row["wallet_score"]),
            condition_id=str(row["signal_condition_id"]),
            token_id=str(row["signal_token_id"]),
            side=str(row["signal_side"]),
            size=float(row["size"]),
            price=float(row["price"]),
            usd_value=float(row["usd_value"]),
            market_title=str(row["market_title"]),
            outcome=str(row["outcome"]),
            market_end_date=(
                datetime.fromisoformat(str(row["market_end_date"]))
                if row["market_end_date"] is not None else None
            ),
            hours_to_resolution=(
                float(row["hours_to_resolution"])
                if row["hours_to_resolution"] is not None else None
            ),
            detected_at=datetime.fromisoformat(str(row["detected_at"])),
            source=str(row["source"]),
            status=str(row["signal_status"]),
            skip_reason=row["skip_reason"],
        )
        return signal, trade

    async def _fill_totals(
        self,
        db: aiosqlite.Connection,
        *,
        copy_trade_id: str | None = None,
        order_id: str | None = None,
    ) -> tuple[float, float]:
        if copy_trade_id is None and order_id is None:
            raise ValueError("copy_trade_id ou order_id é obrigatório")
        field = "copy_trade_id" if copy_trade_id is not None else "order_id"
        value = copy_trade_id if copy_trade_id is not None else order_id
        async with db.execute(
            f"""
            SELECT COALESCE(SUM(size), 0), COALESCE(SUM(size * price), 0)
            FROM copy_trade_fills
            WHERE {field}=?
            """,
            (value,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return 0.0, 0.0
        return float(row[0] or 0.0), float(row[1] or 0.0)
