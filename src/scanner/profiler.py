"""Profiler — transforma payload bruto do leaderboard/trades em métricas."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True, slots=True)
class WalletProfile:
    """Agregado de performance de uma carteira no período."""

    address: str
    name: str | None
    pnl_usd: float
    volume_usd: float
    win_rate: float
    total_trades: int
    distinct_markets: int
    short_term_trade_ratio: float  # % de trades em mercados < 48h
    last_trade_at: datetime | None
    # ⚠️ ALPHA — Recent Performance gate. PnL acumulado dos últimos 7
    # dias. None = não-enriched (compat retroativa). Negativo = whale
    # "on tilt" → scorer aplica 0.3× penalty pra silenciá-la temporariamente.
    recent_pnl_7d: float | None = None

    @property
    def volume_to_pnl_ratio(self) -> float:
        """Regra 3 — Volume-to-PnL: quanto maior, pior (mais wash-trading).

        Calculamos como `|pnl| / volume`. Valores muito baixos indicam que
        a carteira mexe muito dinheiro para gerar pouco lucro — airdrop farmer.
        """
        if self.volume_usd <= 0:
            return 0.0
        return abs(self.pnl_usd) / self.volume_usd


def profile_from_leaderboard_entry(
    entry: dict[str, Any],
    *,
    short_term_threshold_hours: int,
) -> WalletProfile:
    """Constrói WalletProfile a partir de um row do /leaderboard.

    Campos esperados (flexível para variação da API):
        address/proxyWallet, name/pseudonym, pnl/profit/cashPnl,
        volume/volumeUsd, winRate, trades/tradesCount,
        distinctMarkets, shortTermRatio, lastTradeAt.
    """
    addr = entry.get("proxyWallet") or entry.get("address") or entry["user"]
    last = entry.get("lastTradeAt")
    last_dt: datetime | None = None
    if last:
        last_dt = (
            datetime.fromisoformat(last.replace("Z", "+00:00"))
            if isinstance(last, str) else datetime.fromtimestamp(last, tz=timezone.utc)
        )
    return WalletProfile(
        address=addr,
        name=entry.get("name") or entry.get("pseudonym"),
        pnl_usd=float(entry.get("pnl") or entry.get("cashPnl") or entry.get("profit") or 0.0),
        volume_usd=float(entry.get("volume") or entry.get("volumeUsd") or 0.0),
        win_rate=float(entry.get("winRate") or entry.get("win_rate") or 0.0),
        total_trades=int(entry.get("trades") or entry.get("tradesCount") or 0),
        distinct_markets=int(entry.get("distinctMarkets") or entry.get("markets") or 0),
        short_term_trade_ratio=float(entry.get("shortTermRatio") or 0.0),
        last_trade_at=last_dt,
    )
