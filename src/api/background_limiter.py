"""Background rate limiter — separa tráfego de auditoria do hot path HFT.

⚠️ HFT vs AUDITORIA:
    O httpx.AsyncClient singleton é compartilhado pelo bot inteiro.
    Sem este limiter, o Scanner chamando enrich_profiles em rajada
    (22 whales × 3 endpoints = 66 reqs em paralelo) compete pelo mesmo
    pool de conexões HTTP/2 que o post_order do CLOB e a recovery RTDS.
    Resultado: posts de ordem ficam atrás de uma fila de auditoria,
    perdendo ms críticos de inclusão.

    Esta camada limita APENAS chamadas REST de background (Scanner,
    Resolution, snapshot_whale) a um Semaphore configurável (default 5
    concurrent). O hot path (CLOB post_order, RTDS, gamma get_market do
    detect_signal) NÃO passa pelo limiter — sai sem fila.

Uso:
    bg = get_background_limiter()
    async with bg.acquire():
        await client.get("/value", ...)

API singleton — operador customiza no startup via configure().
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from src.core.logger import get_logger

log = get_logger(__name__)

# Default ultra-conservador: 5 reqs em vôo simultâneas. Polymarket
# data-api comporta ~50 reqs/s anônimo; 5 nos deixa MUITO abaixo do
# rate limit, e o hot path nunca compete por essas slots.
DEFAULT_MAX_CONCURRENT = 5
# Timeout no acquire — evita deadlock se o cap virar prison de tasks.
ACQUIRE_TIMEOUT_S = 30.0


class BackgroundRateLimiter:
    """Semaphore wrapper com timeout + métrica."""

    def __init__(self, max_concurrent: int = DEFAULT_MAX_CONCURRENT) -> None:
        self._sem = asyncio.Semaphore(max_concurrent)
        self._max = max_concurrent
        self._waiters = 0
        self._timeouts = 0

    @property
    def max_concurrent(self) -> int:
        return self._max

    @property
    def in_flight(self) -> int:
        # Semaphore._value é o nº de slots LIVRES; in_flight = max - free.
        return self._max - self._sem._value  # type: ignore[attr-defined]

    @property
    def waiters_estimate(self) -> int:
        return self._waiters

    @property
    def timeouts(self) -> int:
        return self._timeouts

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        """Adquire um slot — bloqueia até 30s. Em timeout, log warning
        e PERMITE a chamada seguir (degradação graceful: melhor uma
        chamada fora do limiter do que a auditoria toda parar)."""
        self._waiters += 1
        try:
            try:
                await asyncio.wait_for(self._sem.acquire(), timeout=ACQUIRE_TIMEOUT_S)
                acquired = True
            except asyncio.TimeoutError:
                self._timeouts += 1
                log.warning(
                    "bg_limiter_timeout_bypassed",
                    in_flight=self.in_flight, waiters=self._waiters,
                    timeouts_total=self._timeouts,
                )
                acquired = False
        finally:
            self._waiters -= 1

        try:
            yield
        finally:
            if acquired:
                self._sem.release()


# Singleton — instanciado lazy via get_background_limiter().
_singleton: BackgroundRateLimiter | None = None


def get_background_limiter() -> BackgroundRateLimiter:
    global _singleton
    if _singleton is None:
        _singleton = BackgroundRateLimiter()
    return _singleton


def configure_background_limiter(max_concurrent: int) -> BackgroundRateLimiter:
    """Override singleton com cap customizado. Chamado uma vez no
    main.amain() depois de carregar config."""
    global _singleton
    _singleton = BackgroundRateLimiter(max_concurrent=max_concurrent)
    log.info("bg_limiter_configured", max_concurrent=max_concurrent)
    return _singleton
