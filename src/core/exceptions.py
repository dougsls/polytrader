"""Árvore de exceções do PolyTrader.

Hierarquia:
    PolyTraderError
      ├── PolymarketAPIError        — qualquer 4xx/5xx da Polymarket (com contexto)
      │     ├── RateLimitError      — 429, carrega retry_after
      │     ├── EngineRestart425Error — 425 persistente após backoff esgotar
      │     └── InsufficientFundsError — saldo/allowance insuficiente
      └── SlippageExceededError     — best_ask > whale_price × (1 + tolerance)

Regra de uso: NUNCA capturar genérico. Cada site de erro escolhe a exceção
mais específica. No executor, cada except desliga uma parte diferente do bot
(halt, skip, retry).
"""
from __future__ import annotations


class PolyTraderError(Exception):
    """Base para toda exceção do domínio do bot."""


class PolymarketAPIError(PolyTraderError):
    def __init__(
        self,
        message: str,
        *,
        endpoint: str | None = None,
        status: int | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.status = status
        self.detail = detail


class RateLimitError(PolymarketAPIError):
    def __init__(self, message: str, *, retry_after: float = 1.0, **kw: object) -> None:
        super().__init__(message, **kw)  # type: ignore[arg-type]
        self.retry_after = retry_after


class EngineRestart425Error(PolymarketAPIError):
    """Matching engine devolveu 425 além do teto de retries do backoff."""


class InsufficientFundsError(PolymarketAPIError):
    """USDC insuficiente ou allowance não concedida ao exchange."""


class SlippageExceededError(PolyTraderError):
    """Regra 1 — abortar cópia: mercado se moveu além do tolerável."""

    def __init__(self, *, actual: float, tolerance: float) -> None:
        super().__init__(
            f"slippage {actual:.4%} > tolerance {tolerance:.4%}"
        )
        self.actual = actual
        self.tolerance = tolerance
