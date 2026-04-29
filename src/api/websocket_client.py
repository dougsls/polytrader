"""WebSocket client — RTDS (activity) e Market channel.

Duas características não-negociáveis:
  1. Parser **orjson** (Regra 4): json nativo entope CPU em picos de
     3k msgs/s. orjson é ~3× mais rápido e libera 2× mais memória.
  2. Filtro **Set()** por maker (Regra 4): antes de repassar payload para
     qualquer consumer, checar se `maker` ∈ `tracked_wallets`. Pacotes
     de carteiras não-rastreadas são dropados em nanossegundos.

Também integra o HeartbeatWatchdog (Diretiva 3). Para o market channel
a Polymarket pede PING custom a cada 10s; aqui usamos o watchdog.
"""
from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Callable
from typing import Any

import orjson
import websockets

from src.api.heartbeat import HeartbeatWatchdog
from src.core.logger import get_logger

log = get_logger(__name__)

RTDS_URL = "wss://ws-live-data.polymarket.com"
MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# HFT — reconect agressivo. Em vez de exp-backoff até 60s (que cega o
# bot por 1min após blip de rede), usa jitter constante de 50-150ms.
# Com Polymarket aceitando reconexão sem rate-limit em < ~10/s, isso
# é seguro e elimina latência de detecção.
RECONNECT_JITTER_RANGE = (0.05, 0.15)
# Silence threshold (zombie detection): se RTDS não emitir nada por
# este número de segundos, dropamos e reconectamos. Polymarket emite
# trades de forma contínua durante horário ativo; silêncio = TCP-zombie.
RTDS_SILENCE_THRESHOLD = 3.0


class RTDSClient:
    """Subscription em activity/trades com filtro por maker.

    Args:
        tracked_wallets: referência viva ao Set de carteiras. O scanner
            atualiza esse Set quando o pool muda; o client vê mudanças
            sem reconectar (identidade do Set é preservada).
    """

    PING_INTERVAL = 10.0

    def __init__(
        self,
        tracked_wallets: set[str],
        *,
        url: str = RTDS_URL,
        on_trade: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._tracked = tracked_wallets
        self._url = url
        self._on_trade = on_trade
        self._stop = asyncio.Event()
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._watchdog: HeartbeatWatchdog | None = None

    async def _send_ping(self) -> None:
        if self._ws is None or (self._ws.state.name in ("CLOSED", "CLOSING")):
            raise RuntimeError("ws not connected")
        await self._ws.send("PING")

    async def _on_ping_fail(self) -> None:
        log.warning("rtds_ping_failed_restart_stream")
        if self._ws is not None and self._ws.state.name not in ("CLOSED", "CLOSING"):
            await self._ws.close()

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        """Gerador async. Reconect agressivo (jitter 50-150ms) + zombie watchdog.

        HFT — eliminado o exp-backoff (até 60s cego). Em prediction
        markets, perder 60s = perder múltiplas oportunidades de copy
        em rajadas de whale. Polymarket aceita reconect frequente sem
        rate-limit; jitter constante evita herd-thunder em incidente
        regional.

        Zombie detection: o watchdog dispara reconnect se não recebermos
        NADA do stream por RTDS_SILENCE_THRESHOLD segundos. Polymarket é
        altamente líquido (~3k msgs/s pico); silêncio é sinal de TCP morto.
        """
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self._url, ping_interval=None, close_timeout=5
                ) as ws:
                    self._ws = ws
                    await ws.send(
                        orjson.dumps(
                            {"subscriptions": [{"topic": "activity", "type": "trades"}]}
                        ).decode()
                    )
                    self._watchdog = HeartbeatWatchdog(
                        send_heartbeat=self._send_ping,
                        on_failure=self._on_ping_fail,
                        interval_seconds=self.PING_INTERVAL,
                        silence_threshold_seconds=RTDS_SILENCE_THRESHOLD,
                    )
                    self._watchdog.start()
                    log.info("rtds_connected", silence_threshold=RTDS_SILENCE_THRESHOLD)
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        # Zombie detection: notify ANTES do filtro Set —
                        # qualquer payload conta como prova de vida.
                        self._watchdog.notify_message_received()
                        try:
                            msg = orjson.loads(raw)
                        except orjson.JSONDecodeError:
                            continue
                        # Regra 4 — filtro Set() em nanosegundos. orjson
                        # (Rust) é mais rápido que qualquer prefiltro em
                        # Python: testado empiricamente. Set lookup O(1).
                        maker = (
                            msg.get("maker")
                            or msg.get("makerAddress")
                            or (msg.get("payload") or {}).get("maker")
                            or (msg.get("data") or {}).get("maker")
                        )
                        # Normaliza para lowercase — TARGET_WHALES é sempre
                        # lowercase; RTDS às vezes devolve checksum case.
                        if maker is None or maker.lower() not in self._tracked:
                            continue
                        inner = msg.get("payload") or msg.get("data")
                        effective = inner if isinstance(inner, dict) and "maker" in inner else msg
                        if self._on_trade:
                            self._on_trade(effective)
                        yield effective
            except (websockets.WebSocketException, OSError) as e:
                log.warning("rtds_disconnected_instant_reconnect", err=repr(e))
            finally:
                if self._watchdog is not None:
                    await self._watchdog.stop()
                    self._watchdog = None
                self._ws = None
            if self._stop.is_set():
                break
            # HFT — micro-jitter constante (50-150ms). Sem exp-backoff.
            # Jitter é só p/ thunder-herd em outage Cloudflare regional.
            sleep_for = random.uniform(*RECONNECT_JITTER_RANGE)
            await asyncio.sleep(sleep_for)

    async def close(self) -> None:
        self._stop.set()
        if self._ws is not None and self._ws.state.name not in ("CLOSED", "CLOSING"):
            await self._ws.close()
