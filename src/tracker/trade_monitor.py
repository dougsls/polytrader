"""Trade monitor — consome RTDS filtrado + polling como fallback.

Gera TradeSignal via signal_detector e enfileira em asyncio.Queue para o
executor. Deduplicação por (wallet, condition, token, side, timestamp).
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from pathlib import Path

import aiosqlite

from src.core import metrics

from src.api.data_client import DataAPIClient
from src.api.gamma_client import GammaAPIClient
from src.api.websocket_client import RTDSClient
from src.core.config import TrackerConfig
from src.core.database import DEFAULT_DB_PATH
from src.core.logger import get_logger
from src.core.state import InMemoryState
from src.tracker.signal_detector import detect_signal

log = get_logger(__name__)


class TradeMonitor:
    def __init__(
        self,
        *,
        cfg: TrackerConfig,
        data_client: DataAPIClient,
        gamma: GammaAPIClient,
        ws_client: RTDSClient,
        wallet_scores: dict[str, float],
        queue: asyncio.Queue,
        db_path: Path = DEFAULT_DB_PATH,
        conn: aiosqlite.Connection | None = None,
        state: InMemoryState | None = None,
        wallet_portfolios: dict[str, float] | None = None,
    ) -> None:
        self._cfg = cfg
        self._data = data_client
        self._gamma = gamma
        self._ws = ws_client
        self._scores = wallet_scores
        self._portfolios = wallet_portfolios or {}
        self._queue = queue
        self._db_path = db_path
        self._conn = conn
        self._state = state
        # OrderedDict como LRU bounded — evita memory leak em runs longos.
        # Key = dedup tuple, value = timestamp de inserção (para purga por idade).
        self._seen: OrderedDict[tuple[str, str, str, str, int], float] = OrderedDict()
        self._seen_max_size = 50_000

    def _dedup_key(self, trade: dict) -> tuple[str, str, str, str, int]:
        # Fallback time.monotonic_ns() garante unicidade quando o feed
        # (replay ou payload seco) não traz timestamp — sem isso, trades
        # sem ts viram todos (…, 0) e colidem como duplicatas.
        ts = trade.get("timestamp") or trade.get("time") or time.monotonic_ns()
        return (
            (trade.get("maker") or trade.get("user") or "").lower(),
            trade.get("conditionId") or trade.get("condition_id") or "",
            trade.get("asset") or trade.get("tokenId") or "",
            (trade.get("side") or "").upper(),
            int(ts),
        )

    def _remember(self, key: tuple[str, str, str, str, int]) -> None:
        """Insere com timestamp + purga entries > signal_max_age OU bound de tamanho."""
        now = time.monotonic()
        self._seen[key] = now
        cutoff = now - float(self._cfg.signal_max_age_seconds)
        # Purge por idade (percorre do mais antigo; OrderedDict mantém ordem de insert).
        while self._seen:
            oldest_key = next(iter(self._seen))
            if self._seen[oldest_key] >= cutoff:
                break
            self._seen.popitem(last=False)
        # Bound absoluto — safety net se signal_max_age_seconds for muito alto.
        while len(self._seen) > self._seen_max_size:
            self._seen.popitem(last=False)

    async def _enqueue_trade(self, trade: dict, source: str) -> None:
        """Fluxo comum: dedup → detect_signal → enqueue com backpressure."""
        key = self._dedup_key(trade)
        if key in self._seen:
            return
        self._remember(key)
        wallet = key[0]
        wallet_lower = wallet.lower() if wallet else ""
        signal = await detect_signal(
            trade=trade,
            wallet_score=self._scores.get(wallet_lower, 0.0),
            whale_portfolio_usd=self._portfolios.get(wallet_lower),
            cfg=self._cfg, gamma=self._gamma, data_client=self._data,
            db_path=self._db_path, conn=self._conn, state=self._state,
        )
        if not signal:
            return
        try:
            self._queue.put_nowait(signal)
            metrics.signals_received.inc()
            metrics.queue_size.set(self._queue.qsize())
            log.info("signal_queued", id=signal.id, side=signal.side, source=source)
        except asyncio.QueueFull:
            try:
                dropped = self._queue.get_nowait()
                self._queue.task_done()
                self._queue.put_nowait(signal)
                metrics.signals_dropped.inc()
                log.warning("signal_dropped_due_to_backpressure",
                            dropped_id=dropped.id, kept_id=signal.id)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                metrics.signals_dropped.inc()

    async def run_polling(self, active_wallets: set[str]) -> None:
        """Fallback: polling /trades?user=ADDR a cada poll_interval_seconds.

        Complementar ao WS. Se a whale operar, polling detecta em <=15s.
        Dedup compartilhado com WS impede duplicatas quando ambos ativos.
        """
        interval = float(self._cfg.poll_interval_seconds)
        log.info("polling_loop_start", interval_s=interval)
        last_seen_ts: dict[str, int] = {}
        while True:
            try:
                whales = list(active_wallets)
                if not whales:
                    await asyncio.sleep(interval)
                    continue
                # Paralelo: pega últimos N trades de cada whale.
                results = await asyncio.gather(
                    *[self._data.trades(w, limit=10) for w in whales],
                    return_exceptions=True,
                )
                for wallet, res in zip(whales, results, strict=False):
                    if isinstance(res, Exception):
                        log.warning("polling_fetch_failed",
                                    addr=wallet[:10], err=repr(res))
                        continue
                    last_ts = last_seen_ts.get(wallet, 0)
                    max_ts = last_ts
                    # Polymarket /trades returns newest-first, mas garantimos.
                    for t in res:
                        ts_raw = t.get("timestamp") or t.get("time") or 0
                        try:
                            ts = int(ts_raw)
                        except (TypeError, ValueError):
                            ts = 0
                        if ts <= last_ts:
                            continue
                        if ts > max_ts:
                            max_ts = ts
                        # Injeta maker se ausente (endpoint retorna proxyWallet)
                        if "maker" not in t:
                            t["maker"] = wallet
                        try:
                            await self._enqueue_trade(t, source="polling")
                        except Exception as exc:  # noqa: BLE001
                            # Mercado unknown na Gamma, spread falho, etc.
                            # Não queremos que 1 trade ruim quebre o loop de 20 whales.
                            log.info("polling_trade_skipped",
                                     wallet=wallet[:10], err=str(exc)[:60])
                    last_seen_ts[wallet] = max_ts
            except Exception as exc:  # noqa: BLE001
                log.error("polling_loop_crash", err=repr(exc))
            await asyncio.sleep(interval)

    async def run_websocket(self) -> None:
        """WebSocket RTDS — path primário (quando o endpoint está OK)."""
        async for trade in self._ws.stream():
            await self._enqueue_trade(trade, source="websocket")
