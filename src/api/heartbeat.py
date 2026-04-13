"""Diretiva HFT 3 — Watchdog de Heartbeat do Websocket L2 CLOB.

A Polymarket tem política de *Cancel on Disconnect*: se o bot perder
latência no websocket L2, ordens limit abertas são canceladas. Isso é
catastrófico para copy-trading — a posição fica desincronizada com a baleia.

Este watchdog roda como coroutine em paralelo ao orquestrador. Dispara
postHeartbeat no intervalo configurado e, após N falhas consecutivas,
invoca o callback de restauração (`on_failure`) que deve reconectar o
websocket e re-submeter ordens abertas a partir do estado do DB.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 10.0
DEFAULT_FAILURE_THRESHOLD = 3


class HeartbeatWatchdog:
    """Envia heartbeats periódicos e aciona reconexão após N falhas.

    Args:
        send_heartbeat: corrotina que chama postHeartbeat no CLOB.
            Deve levantar exceção em caso de falha (timeout, 4xx, 5xx).
        on_failure: corrotina disparada após `failure_threshold` falhas
            consecutivas. Tipicamente chama websocket_client.reconnect() +
            executor.restore_open_orders().
        interval_seconds: período entre heartbeats. Polymarket documenta
            ~10s como padrão seguro.
        failure_threshold: falhas consecutivas tolerados antes de acionar
            a restauração.
    """

    def __init__(
        self,
        send_heartbeat: Callable[[], Awaitable[None]],
        on_failure: Callable[[], Awaitable[None]],
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
    ) -> None:
        self._send = send_heartbeat
        self._on_failure = on_failure
        self._interval = interval_seconds
        self._threshold = failure_threshold
        self._consecutive_failures = 0
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        """Loop principal. Encerra em shutdown via stop()."""
        while not self._stop.is_set():
            try:
                await self._send()
                if self._consecutive_failures:
                    logger.info(
                        "heartbeat_recovered after=%d failures",
                        self._consecutive_failures,
                    )
                self._consecutive_failures = 0
            except Exception as exc:  # noqa: BLE001 — qualquer falha conta
                self._consecutive_failures += 1
                logger.warning(
                    "heartbeat_failed consecutive=%d err=%r",
                    self._consecutive_failures,
                    exc,
                )
                if self._consecutive_failures >= self._threshold:
                    logger.error("heartbeat_threshold_exceeded triggering restore")
                    try:
                        await self._on_failure()
                    except Exception:  # noqa: BLE001
                        logger.exception("heartbeat_restore_failed")
                    self._consecutive_failures = 0
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass

    def start(self) -> asyncio.Task[None]:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="heartbeat-watchdog")
        return self._task

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
