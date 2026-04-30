"""Telegram notifier — fire-and-forget, não bloqueia o executor.

Guard: se `token` ou `chat_id` vazios, todas as mensagens viram no-op
(log warning). Permite rodar em dev sem credenciais. Em produção na
VPS, configure .env e reinicie.

⚠️ HTML PARSE — `_send` agora dispara com parse_mode="HTML". Templates
usam `<b>`, `<i>` etc. Campos vindos de fonte externa (market_title,
outcome, wallet_address) passam por html.escape() para evitar que
títulos com `<` ou `>` quebrem o parse e a mensagem inteira falhe.
"""
from __future__ import annotations

import asyncio
import html
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
                chat_id=self._chat_id, text=text,
                disable_notification=silent,
                parse_mode="HTML",
            )
        except Exception as exc:  # noqa: BLE001 — nunca derrubar executor
            log.warning("telegram_send_failed", err=repr(exc))

    def notify(self, text: str, *, silent: bool = False) -> asyncio.Task[None]:
        """Fire-and-forget: retorna a Task sem aguardar."""
        return asyncio.create_task(self._send(text, silent=silent))

    def notify_trade(self, signal: TradeSignal, trade: CopyTrade) -> asyncio.Task[None]:
        emoji = "🟢" if signal.side == "BUY" else "🔴"
        # Sanitize campos USER-PROVIDED — títulos podem ter <, >, & que
        # quebrariam o parse HTML do Telegram. Strings template (<b>...) ficam.
        title_safe = html.escape(signal.market_title[:60])
        outcome_safe = html.escape(signal.outcome)
        wallet_safe = html.escape(signal.wallet_address[:10])
        text = (
            f"{emoji} <b>{signal.side}</b> {title_safe}\n"
            f"Outcome: {outcome_safe} @ {trade.intended_price:.4f}\n"
            f"Size: {trade.intended_size:.2f} (${trade.intended_size * trade.intended_price:.2f})\n"
            f"Whale: {wallet_safe}… (score {signal.wallet_score:.2f})\n"
            f"Hours-to-res: {signal.hours_to_resolution:.1f}h"
            if signal.hours_to_resolution is not None else
            f"{emoji} <b>{signal.side}</b> {title_safe} @ {trade.intended_price:.4f}"
        )
        return self.notify(text)

    def notify_risk(self, reason: str) -> asyncio.Task[None]:
        return self.notify(f"⚠️ RISK ALERT: {html.escape(reason)}")

    def notify_skip(self, signal: TradeSignal, reason: str) -> asyncio.Task[None]:
        title_safe = html.escape(signal.market_title[:50])
        reason_safe = html.escape(reason)
        return self.notify(
            f"⏭️ SKIP {signal.side} {title_safe} — {reason_safe}",
            silent=True,
        )
