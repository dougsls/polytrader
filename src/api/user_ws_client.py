"""M10 — User WebSocket channel do CLOB.

Hoje consumimos só o RTDS público (trades de todo mundo). Fills reais
das NOSSAS ordens (incluindo preenchimentos parciais) não são capturados.
Resultado: se postamos 100 mas encheu 60, gravamos 100 em `bot_positions`.

Este client subscreve o user channel do CLOB com as credenciais L2:
    {"type": "user", "auth": {...}, "markets": [...]}

Eventos recebidos:
    - `order`: status updates (pending/live → partial/filled/cancelled/rejected)
    - `trade`: fill real com `size_matched`, `price`, `maker`, `taker_orders`

Integração: passamos um callback `on_event(msg)` que persiste o ciclo
da ordem e aplica fills confirmados.
"""
from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Any

import orjson
import websockets

from src.api.auth import L2Credentials
from src.core.logger import get_logger

log = get_logger(__name__)

USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"

UserEventCallback = Callable[[dict[str, Any]], Awaitable[None]]


class UserWSClient:
    """Subscreve canal user do CLOB e propaga fills reais.

    Args:
        credentials: L2 creds obtidas do prefetch no startup.
        on_event: corotina chamada com cada evento `order`/`trade`.
        condition_ids: conjunto vivo de markets a subscrever (mutável).
    """

    def __init__(
        self,
        *,
        credentials: L2Credentials,
        on_event: UserEventCallback,
        condition_ids: set[str],
    ) -> None:
        self._creds = credentials
        self._on_event = on_event
        self._condition_ids = condition_ids
        self._stop = asyncio.Event()
        self._resubscribe = asyncio.Event()
        self._ws: websockets.WebSocketClientProtocol | None = None

    async def _send_subscription(self) -> None:
        if self._ws is None:
            return
        msg = {
            "type": "user",
            "auth": {
                "apiKey": self._creds.api_key,
                "secret": self._creds.secret,
                "passphrase": self._creds.passphrase,
            },
            "markets": list(self._condition_ids),
        }
        await self._ws.send(orjson.dumps(msg).decode())
        self._resubscribe.clear()

    async def refresh_subscription(self) -> None:
        if self._ws is not None:
            await self._send_subscription()
        else:
            self._resubscribe.set()

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    USER_WS_URL, ping_interval=None, close_timeout=5,
                ) as ws:
                    self._ws = ws
                    await self._send_subscription()
                    backoff = 1.0
                    while not self._stop.is_set():
                        if self._resubscribe.is_set():
                            await self._send_subscription()
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        except asyncio.TimeoutError:
                            continue
                        except websockets.ConnectionClosed:
                            break
                        try:
                            msg = orjson.loads(raw)
                        except orjson.JSONDecodeError:
                            continue
                        event_type = msg.get("event_type") or msg.get("type")
                        if event_type not in {"trade", "order"}:
                            continue
                        try:
                            await self._on_event(msg)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("user_ws_on_event_failed", err=repr(exc))
            except (websockets.WebSocketException, OSError) as e:
                log.warning("user_ws_disconnected", err=repr(e), backoff=backoff)
            self._ws = None
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff + random.uniform(0, backoff * 0.25))
            backoff = min(backoff * 2, 60.0)

    async def close(self) -> None:
        self._stop.set()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
