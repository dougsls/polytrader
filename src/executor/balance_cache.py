"""Phase 3 — Cache local de saldo USDC.e (Polygon).

Objetivo: o risk_manager consulta saldo para dimensionar trades. Fazer
RPC a cada sinal custa ~200ms. Em vez disso, uma task background
atualiza o cache a cada N segundos; o hot path lê direto da RAM.

Write-through simples: cache = {"usdc": float, "updated_at": datetime}.
Se a task parar de atualizar por > 60s, o risk_manager deve alertar.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from src.core.logger import get_logger

log = get_logger(__name__)

REFRESH_INTERVAL_SECONDS = 15.0
STALE_THRESHOLD_SECONDS = 60.0


class BalanceCache:
    """Cache em RAM do saldo USDC disponível para o bot.

    Também mantém o **peak portfolio value** (RAM, sobrevive bootstrap
    via `risk_snapshots`) — usado pelo `build_risk_state` para calcular
    drawdown CORRETO (peak-to-trough) em vez de loss instantâneo.
    """

    def __init__(self, fetch_balance: Callable[[], Awaitable[float]]) -> None:
        self._fetch = fetch_balance
        self._balance_usdc: float = 0.0
        self._updated_at: datetime | None = None
        self._stop = asyncio.Event()
        # Peak portfolio value — atualizado externamente via
        # `note_portfolio_value`. Bootstrap em main.py via SELECT MAX(...)
        # de risk_snapshots para sobreviver restart.
        self._peak_portfolio_value: float = 0.0

    @property
    def balance_usdc(self) -> float:
        return self._balance_usdc

    @property
    def is_fresh(self) -> bool:
        if self._updated_at is None:
            return False
        age = (datetime.now(timezone.utc) - self._updated_at).total_seconds()
        return age <= STALE_THRESHOLD_SECONDS

    async def refresh_once(self) -> None:
        try:
            value = await self._fetch()
        except Exception as exc:  # noqa: BLE001 — cache é resiliente; log e segue
            log.warning("balance_fetch_failed", err=repr(exc))
            return
        self._balance_usdc = value
        self._updated_at = datetime.now(timezone.utc)
        log.info("balance_refreshed", usdc=value)

    async def run_loop(self) -> None:
        await self.refresh_once()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=REFRESH_INTERVAL_SECONDS,
                )
            except asyncio.TimeoutError:
                await self.refresh_once()

    async def stop(self) -> None:
        self._stop.set()

    def age(self) -> timedelta | None:
        if self._updated_at is None:
            return None
        return datetime.now(timezone.utc) - self._updated_at

    # --- Peak portfolio tracking (drawdown correto) ----------------------

    @property
    def peak_portfolio_value(self) -> float:
        return self._peak_portfolio_value

    def note_portfolio_value(self, value: float) -> float:
        """Marca observação de portfolio_value e atualiza o peak se for
        novo máximo. Retorna o peak atual.

        Drawdown correto = (peak - current) / peak — mas para isso, peak
        precisa refletir o MAIOR valor já observado, NÃO um snapshot
        instantâneo da entrada.

        Defesa: só registra valores positivos e quando balance cache está
        fresh; outliers de cache stale (balance=0) não viciam o peak.
        """
        if value > 0 and self.is_fresh and value > self._peak_portfolio_value:
            self._peak_portfolio_value = value
        return self._peak_portfolio_value

    def bootstrap_peak(self, peak: float) -> None:
        """Inicializa peak no startup via SELECT MAX(total_portfolio_value)
        em risk_snapshots. Sobrevive restart sem perder o histórico."""
        if peak > self._peak_portfolio_value:
            self._peak_portfolio_value = peak
