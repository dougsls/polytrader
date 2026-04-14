"""httpx.AsyncClient singleton com keep-alive, HTTP/2 e pre-warm.

Motivo: RTT Dublin→Polymarket-edge (Cloudflare) é ~18ms. Cada request
que abre nova conexão TCP+TLS paga ~30-50ms extras. Mantemos o mesmo
client em todo o código, com HTTP/2 multiplexado e pool pre-aquecido
no startup (elimina cold-start spike na primeira ordem real).
"""
from __future__ import annotations

import asyncio
from typing import Final

import httpx

from src.core.logger import get_logger

log = get_logger(__name__)

_client: httpx.AsyncClient | None = None
_lock = asyncio.Lock()

_DEFAULT_LIMITS: Final = httpx.Limits(
    max_keepalive_connections=20,
    max_connections=40,
    keepalive_expiry=60.0,
)
# Timeout: connect curto (Dublin→Cloudflare), read longo pra WS upgrade
_DEFAULT_TIMEOUT: Final = httpx.Timeout(30.0, connect=10.0)

# Endpoints canônicos pra pre-warm. GET / em cada host abre TCP+TLS,
# cacheia em keepalive. Próximas chamadas reais saltam handshake.
PREWARM_URLS: Final = (
    "https://clob.polymarket.com/",
    "https://data-api.polymarket.com/",
    "https://gamma-api.polymarket.com/",
)


async def get_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        async with _lock:
            if _client is None or _client.is_closed:
                _client = httpx.AsyncClient(
                    timeout=_DEFAULT_TIMEOUT,
                    limits=_DEFAULT_LIMITS,
                    # HTTP/2 multiplexa múltiplas requests sobre 1 TCP.
                    # Polymarket edge (Cloudflare) suporta. Elimina
                    # head-of-line blocking quando scanner+tracker+executor
                    # fazem chamadas concorrentes.
                    http2=True,
                    headers={"User-Agent": "polytrader/0.1"},
                )
    return _client


async def prewarm_connections(urls: tuple[str, ...] = PREWARM_URLS) -> None:
    """Abre TCP+TLS em paralelo para todos os endpoints.

    Chamado uma vez no startup, após check_geoblock + latency_baseline.
    Elimina o cold-start spike (~40ms) da primeira request real do executor.
    """
    client = await get_http_client()
    tasks = [
        asyncio.create_task(client.get(url, timeout=5.0), name=f"prewarm-{url}")
        for url in urls
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for url, res in zip(urls, results, strict=False):
        if isinstance(res, Exception):
            log.warning("prewarm_failed", url=url, err=repr(res))
        else:
            log.info(
                "prewarm_ok", url=url, status=res.status_code,
                http_version=res.http_version,
            )


async def close_http_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
