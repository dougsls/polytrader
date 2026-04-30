"""M10 — User WebSocket channel do CLOB.

Hoje consumimos só o RTDS público (trades de todo mundo). Fills reais
das NOSSAS ordens (incluindo preenchimentos parciais) não são capturados.
Resultado: se postamos 100 mas encheu 60, gravamos 100 em `bot_positions`.

Este client subscreve o user channel do CLOB com as credenciais L2:
    {"type": "user", "auth": {...}, "markets": [...]}

Eventos recebidos:
    - `order`: status updates (pending → matched → filled/cancelled)
    - `trade`: fill real com `size_matched`, `price`, `maker`, `taker_orders`

Integração: passamos um callback `on_fill(token_id, size, price)` que
chama `apply_fill()` com os valores verdadeiros da exchange.
"""
from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Any

import orjson
import websockets  # noqa: F401  # WebSocketException é usado no except block abaixo

from src.api.auth import L2Credentials
from src.core.logger import get_logger

log = get_logger(__name__)

USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"

FillCallback = Callable[[dict[str, Any]], Awaitable[None]]


class UserWSClient:
    """Subscreve canal user do CLOB e propaga fills reais.

    Args:
        credentials: L2 creds obtidas do prefetch no startup.
        on_fill: corotina chamada com cada evento de trade preenchido.
        condition_ids: conjunto vivo de markets a subscrever (mutável).
    """

    def __init__(
        self,
        *,
        credentials: L2Credentials,
        on_fill: FillCallback,
        condition_ids: set[str],
    ) -> None:
        self._creds = credentials
        self._on_fill = on_fill
        self._condition_ids = condition_ids
        self._stop = asyncio.Event()
        # `Any` por compat com versões variantes do websockets onde
        # WebSocketClientProtocol foi reorganizado/deprecated em 14+.
        self._ws: Any = None

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

    async def add_market(self, condition_id: str) -> bool:
        """⚠️ Auto-subscribe: chamado pelo CopyEngine quando uma ordem
        live é submetida em mercado novo (que ainda não estava no set
        inicial). Adiciona ao set vivo e re-envia subscription se o WS
        está conectado. Retorna True se a chamada efetivamente adicionou
        o market (False se já estava presente).

        Sem isso, fills GTC em mercados novos nunca chegariam até o
        próximo restart com reconcile.
        """
        cid = condition_id.lower() if condition_id else ""
        if not cid:
            return False
        if cid in self._condition_ids:
            return False
        self._condition_ids.add(cid)
        # Re-envia subscription apenas se WS conectado. Se WS está
        # reconectando, o set vivo é lido pelo `_send_subscription`
        # automaticamente ao reconectar.
        if self._ws is not None:
            try:
                await self._send_subscription()
                log.info("user_ws_market_added", condition_id=cid[:12])
            except Exception as exc:  # noqa: BLE001 — re-subscribe falha não bloqueia
                log.warning(
                    "user_ws_resubscribe_failed",
                    condition_id=cid[:12], err=repr(exc)[:80],
                )
        return True

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
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        try:
                            msg = orjson.loads(raw)
                        except orjson.JSONDecodeError:
                            continue
                        event_type = msg.get("event_type") or msg.get("type")
                        if event_type != "trade":
                            continue
                        # Eventos de fill: size_matched é a fração real
                        try:
                            await self._on_fill(msg)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("user_ws_on_fill_failed", err=repr(exc))
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
