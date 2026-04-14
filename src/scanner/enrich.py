"""Enriquecimento on-chain de WalletProfiles.

Polymarket removeu `/leaderboard` mas endpoints per-wallet continuam:
    /value?user=ADDR             → posição total em USDC
    /traded?user=ADDR            → contagem de trades
    /closed-positions?user=ADDR  → histórico fechado (realizedPnl, outcome)

Agregamos essas métricas REAIS por whale em paralelo no scanner.tick().
Cache TTL curto (15min) pra não rehammerar em ticks subsequentes do mesmo
whale antes de novos trades serem registrados.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from src.api.data_client import DataAPIClient
from src.core.logger import get_logger
from src.scanner.profiler import WalletProfile

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class EnrichedMetrics:
    pnl_usd: float
    volume_usd: float  # value atual (capital ativo)
    win_rate: float
    total_trades: int
    distinct_markets: int
    last_trade_at: datetime | None


async def _fetch_json(client: DataAPIClient, path: str, **params: Any) -> Any:
    """Wrapper para _get privado do DataAPIClient."""
    return await client._get(path, params=params)  # noqa: SLF001


def _is_win(pos: dict) -> bool:
    """Considera vitória se realizedPnl > 0 numa closed position."""
    try:
        return float(pos.get("realizedPnl") or 0) > 0
    except (TypeError, ValueError):
        return False


async def fetch_metrics(
    client: DataAPIClient, address: str,
) -> EnrichedMetrics:
    """Agrega /value + /traded + /closed-positions para uma whale."""
    addr = address.lower()
    try:
        value_resp, traded_resp, closed = await asyncio.gather(
            _fetch_json(client, "/value", user=addr),
            _fetch_json(client, "/traded", user=addr),
            _fetch_json(client, "/closed-positions", user=addr, limit=100),
            return_exceptions=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("enrich_all_failed", addr=addr[:10], err=repr(exc))
        raise

    # /value → capital ativo
    if isinstance(value_resp, list) and value_resp:
        volume_usd = float(value_resp[0].get("value") or 0)
    else:
        volume_usd = 0.0

    # /traded → contagem total
    if isinstance(traded_resp, dict):
        total_trades = int(traded_resp.get("traded") or 0)
    else:
        total_trades = 0

    # /closed-positions → PnL + win rate + last trade + distinct markets
    pnl_usd = 0.0
    wins = 0
    losses = 0
    markets: set[str] = set()
    last_ts = 0
    if isinstance(closed, list):
        for pos in closed:
            if not isinstance(pos, dict):
                continue
            try:
                pnl_usd += float(pos.get("realizedPnl") or 0)
            except (TypeError, ValueError):
                pass
            if _is_win(pos):
                wins += 1
            else:
                losses += 1
            cid = pos.get("conditionId") or pos.get("condition_id")
            if cid:
                markets.add(cid)
            ts = pos.get("timestamp") or 0
            if isinstance(ts, (int, float)) and ts > last_ts:
                last_ts = int(ts)

    total_closed = wins + losses
    win_rate = (wins / total_closed) if total_closed > 0 else 0.0

    last_trade_at = (
        datetime.fromtimestamp(last_ts, tz=timezone.utc) if last_ts else None
    )

    return EnrichedMetrics(
        pnl_usd=pnl_usd,
        volume_usd=volume_usd,
        win_rate=win_rate,
        total_trades=total_trades,
        distinct_markets=len(markets),
        last_trade_at=last_trade_at,
    )


async def enrich_profiles(
    client: DataAPIClient,
    profiles: list[WalletProfile],
) -> list[WalletProfile]:
    """Substitui os campos sintéticos por dados reais on-chain.

    Se uma enrich falha (rate limit, endpoint down), mantém o profile
    original intacto — evita zerar o pool inteiro por erro transient.
    """
    results = await asyncio.gather(
        *[fetch_metrics(client, p.address) for p in profiles],
        return_exceptions=True,
    )
    out: list[WalletProfile] = []
    for p, res in zip(profiles, results, strict=False):
        if isinstance(res, Exception):
            log.warning("enrich_failed", addr=p.address[:10], err=repr(res))
            out.append(p)
            continue
        out.append(replace(
            p,
            pnl_usd=res.pnl_usd,
            volume_usd=res.volume_usd,
            win_rate=res.win_rate,
            total_trades=res.total_trades,
            distinct_markets=res.distinct_markets,
            last_trade_at=res.last_trade_at or p.last_trade_at,
            # short_term_trade_ratio: mantemos estimativa inicial
        ))
    return out
