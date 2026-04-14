"""Diretiva HFT 2 — Exponential backoff para HTTP 425 (Too Early).

O matching engine da Polymarket entra em restart rápido periodicamente e
devolve HTTP 425. A política determinística é: 1.5s → 3.0s → 6.0s (dobrando)
com teto configurável. Não é retry genérico — só 425. Outros 4xx devem
propagar para diagnóstico imediato.

Uso:
    @retry_on_425()
    async def post_order(self, ...): ...
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, TypeVar

import httpx

from src.core.exceptions import EngineRestart425Error
from src.core.logger import get_logger

logger = get_logger(__name__)

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])

DEFAULT_INITIAL_DELAY = 1.5
DEFAULT_MAX_DELAY = 6.0
DEFAULT_MAX_ATTEMPTS = 4

# Alias retrocompatível — a exceção canônica vive em core/exceptions.
MatchingEngineRestartError = EngineRestart425Error


def retry_on_425(
    initial_delay: float = DEFAULT_INITIAL_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> Callable[[F], F]:
    """Decorador async que captura 425 e aplica backoff exponencial.

    A função alvo deve levantar httpx.HTTPStatusError (ou um wrapper com
    atributo .response.status_code) em caso de erro HTTP.
    """

    def decorator(func: F) -> F:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            delay = initial_delay
            last_exc: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 425:
                        raise
                    last_exc = exc
                    if attempt == max_attempts:
                        break
                    logger.warning(
                        "clob_425_restart", attempt=attempt, delay=delay,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, max_delay)
            raise EngineRestart425Error(
                f"425 persistente após {max_attempts} tentativas",
                status=425,
            ) from last_exc

        return wrapper  # type: ignore[return-value]

    return decorator
