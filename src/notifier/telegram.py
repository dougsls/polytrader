"""Telegram notifier — fire-and-forget, não bloqueia o executor.

Guard: se `token` ou `chat_id` vazios, todas as mensagens viram no-op
(log warning). Permite rodar em dev sem credenciais. Em produção na
VPS, configure .env e reinicie.
"""
from __future__ import annotations

import asyncio
from typing import Any

from src.core.logger import get_logger
from src.core.models import CopyTrade, TradeSignal

log = get_logger(__name__)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str) -> None:
        self._token = token
        self._chat_id = chat_id
        self._bot: Any | None = None
        if token and chat_id:
            # Import lazy: evita exigir python-telegram-bot em testes.
            from telegram import Bot  # type: ignore[import-not-found]

            self._bot = Bot(token=token)

    async def _send(self, text: str, *, silent: bool = False) -> None:
        if self._bot is None:
            log.debug("telegram_noop", text=text[:60])
            return
        try:
            await self._bot.send_message(
                chat_id=self._chat_id, text=text, disable_notification=silent,
            )
        except Exception as exc:  # noqa: BLE001 — nunca derrubar executor
            log.warning("telegram_send_failed", err=repr(exc))

    def notify(self, text: str, *, silent: bool = False) -> asyncio.Task[None]:
        """Fire-and-forget: retorna a Task sem aguardar."""
        return asyncio.create_task(self._send(text, silent=silent))

    def notify_trade(self, signal: TradeSignal, trade: CopyTrade) -> asyncio.Task[None]:
        emoji = "🟢" if signal.side == "BUY" else "🔴"
        text = (
            f"{emoji} <b>{signal.side}</b> {signal.market_title[:60]}\n"
            f"Outcome: {signal.outcome} @ {trade.intended_price:.4f}\n"
            f"Size: {trade.intended_size:.2f} (${trade.intended_size * trade.intended_price:.2f})\n"
            f"Whale: {signal.wallet_address[:10]}… (score {signal.wallet_score:.2f})\n"
            f"Hours-to-res: {signal.hours_to_resolution:.1f}h"
            if signal.hours_to_resolution is not None else
            f"{emoji} <b>{signal.side}</b> {signal.market_title[:60]} @ {trade.intended_price:.4f}"
        )
        return self.notify(text)

    def notify_risk(self, reason: str) -> asyncio.Task[None]:
        return self.notify(f"⚠️ RISK ALERT: {reason}")

    def notify_skip(self, signal: TradeSignal, reason: str) -> asyncio.Task[None]:
        return self.notify(
            f"⏭️ SKIP {signal.side} {signal.market_title[:50]} — {reason}",
            silent=True,
        )
