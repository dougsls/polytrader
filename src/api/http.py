"""httpx.AsyncClient singleton com keep-alive e pooling.

Motivo: RTT NY→London é 70-130ms. Cada request que abre nova conexão TCP
paga ~100ms extras de handshake (TCP + TLS). Usar o mesmo client em todo
o código economiza uma conexão por request e estabiliza latência.
"""
from __future__ import annotations

import asyncio
from typing import Final

import httpx

_client: httpx.AsyncClient | None = None
_lock = asyncio.Lock()

_DEFAULT_LIMITS: Final = httpx.Limits(
    max_keepalive_connections=20,
    max_connections=40,
    keepalive_expiry=60.0,
)
_DEFAULT_TIMEOUT: Final = httpx.Timeout(30.0, connect=10.0)


async def get_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        async with _lock:
            if _client is None or _client.is_closed:
                _client = httpx.AsyncClient(
                    timeout=_DEFAULT_TIMEOUT,
                    limits=_DEFAULT_LIMITS,
                    http2=False,  # Polymarket não fala HTTP/2 confiável
                    headers={"User-Agent": "polytrader/0.1"},
                )
    return _client


async def close_http_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
