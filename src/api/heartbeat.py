"""Diretiva HFT 3 — Watchdog de Heartbeat + Zombie Connection Detection.

A Polymarket tem política de *Cancel on Disconnect*: se o bot perder
latência no websocket L2, ordens limit abertas são canceladas. Isso é
catastrófico para copy-trading — a posição fica desincronizada com a baleia.

DUAS detecções complementares:
  1. **Ping fail** — heartbeat HTTP/WS fail por N tentativas (legacy).
  2. **Silence/zombie** — RTDS receber zero mensagens por X segundos.
     Polymarket é altamente líquido (~3k msgs/s em pico). Silêncio >3s
     em horário comercial = TCP-zombie (conexão aceita TCP keep-alive
     mas o stream parou). Sem essa detecção, o bot espera 10s+ do ping
     timeout cego enquanto perde alpha.

Este watchdog roda como coroutine em paralelo ao orquestrador. Dispara
postHeartbeat no intervalo configurado e, após N falhas consecutivas
OU silêncio acima do threshold, invoca o callback de restauração
(`on_failure`) que deve reconectar o websocket.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 10.0
DEFAULT_FAILURE_THRESHOLD = 3
# Silence detection — None desliga; 3.0 é o default HFT agressivo.
DEFAULT_SILENCE_THRESHOLD: float | None = None
# Frequência de checagem do zombie — 500ms é fine-grained suficiente.
SILENCE_CHECK_INTERVAL = 0.5


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
        silence_threshold_seconds: float | None = DEFAULT_SILENCE_THRESHOLD,
    ) -> None:
        self._send = send_heartbeat
        self._on_failure = on_failure
        self._interval = interval_seconds
        self._threshold = failure_threshold
        self._consecutive_failures = 0
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        # Zombie detection — None desliga.
        self._silence_threshold = silence_threshold_seconds
        # monotonic() é safe contra ajuste de relógio (NTP saltos).
        # None = ainda nenhum sinal recebido (não dispara antes do 1º msg).
        self._last_message_at: float | None = None
        self._silence_task: asyncio.Task[None] | None = None

    def notify_message_received(self) -> None:
        """RTDSClient chama isto a cada payload recebido (antes do filtro Set).

        Atualiza o timestamp do último contato. Sub-microsegundo — pode
        ser chamado dentro do hot path sem afetar throughput do RTDS.
        """
        self._last_message_at = time.monotonic()

    async def _silence_loop(self) -> None:
        """Loop secundário de detecção de TCP-zombie. Independente do ping."""
        if self._silence_threshold is None:
            return
        # Aguarda o primeiro sinal antes de começar a contar — evita disparar
        # falso-positivo no startup antes da subscription confirmar.
        while not self._stop.is_set() and self._last_message_at is None:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=SILENCE_CHECK_INTERVAL)
            except asyncio.TimeoutError:
                pass
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=SILENCE_CHECK_INTERVAL)
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                return
            last = self._last_message_at
            if last is None:
                continue
            silence = time.monotonic() - last
            if silence > self._silence_threshold:
                logger.error(
                    "ws_zombie_detected silence=%.1fs threshold=%.1fs — forcing reconnect",
                    silence, self._silence_threshold,
                )
                # Reseta antes de chamar callback pra não disparar 2x se
                # a reconexão demorar mais que SILENCE_CHECK_INTERVAL.
                self._last_message_at = None
                try:
                    await self._on_failure()
                except Exception:  # noqa: BLE001
                    logger.exception("zombie_restore_failed")

    async def run(self) -> None:
        """Loop principal. Encerra em shutdown via stop()."""
        # Boota o silence loop (no-op se threshold=None).
        if self._silence_threshold is not None:
            self._silence_task = asyncio.create_task(
                self._silence_loop(), name="heartbeat-silence",
            )
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
        if self._silence_task:
            await self._silence_task
