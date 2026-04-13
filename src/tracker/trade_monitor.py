"""Trade monitor — consome RTDS filtrado + polling como fallback.

Gera TradeSignal via signal_detector e enfileira em asyncio.Queue para o
executor. Deduplicação por (wallet, condition, token, side, timestamp).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from src.api.data_client import DataAPIClient
from src.api.gamma_client import GammaAPIClient
from src.api.websocket_client import RTDSClient
from src.core.config import TrackerConfig
from src.core.database import DEFAULT_DB_PATH
from src.core.logger import get_logger
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
    ) -> None:
        self._cfg = cfg
        self._data = data_client
        self._gamma = gamma
        self._ws = ws_client
        self._scores = wallet_scores
        self._queue = queue
        self._db_path = db_path
        self._seen: set[tuple[str, str, str, str, int]] = set()

    def _dedup_key(self, trade: dict) -> tuple[str, str, str, str, int]:
        return (
            trade.get("maker") or trade.get("user") or "",
            trade.get("conditionId") or trade.get("condition_id") or "",
            trade.get("asset") or trade.get("tokenId") or "",
            (trade.get("side") or "").upper(),
            int(trade.get("timestamp") or trade.get("time") or 0),
        )

    async def run_websocket(self) -> None:
        async for trade in self._ws.stream():
            key = self._dedup_key(trade)
            if key in self._seen:
                continue
            self._seen.add(key)
            wallet = key[0]
            signal = await detect_signal(
                trade=trade,
                wallet_score=self._scores.get(wallet, 0.0),
                cfg=self._cfg,
                gamma=self._gamma,
                data_client=self._data,
                db_path=self._db_path,
            )
            if signal:
                await self._queue.put(signal)
                log.info("signal_queued", id=signal.id, side=signal.side)
