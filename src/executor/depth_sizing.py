"""Track B — Sizing book-aware para o copy-trader.

Em vez de só validar o anchor da whale (Regra 1), lê o livro real e
calcula:
  - Quanto USD consegue encher percorrendo até max_levels níveis
  - Qual é o preço médio efetivo (VWAP) desse fill
  - Qual é o impacto vs midpoint atual

Saída: max_size_usd que respeita max_impact_pct.

Quando habilitado em config, o copy-engine corta o tamanho do trade pelo
menor entre `whale_proportional_size` e `depth_aware_size`. Isso impede
postar GTC pendurado em book raso ou pagar slippage absurdo em FOK.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.core.logger import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DepthQuote:
    """Resultado da análise de book."""

    fillable_size_usd: float       # quanto cabe sem violar max_impact
    vwap_price: float              # preço médio do fill simulado
    levels_consumed: int           # quantos níveis encheu
    best_price: float              # preço da melhor oferta
    impact_pct: float              # (vwap - best) / best
    book_thin: bool                # True se book < 2 níveis úteis


def quote_buy_depth(
    book: dict[str, Any],
    max_size_usd: float,
    max_impact_pct: float,
    max_levels: int = 5,
) -> DepthQuote:
    """Simula um BUY consumindo asks até max_size_usd OU max_impact.

    `book` no shape Polymarket: {"asks": [{"price","size"}], "bids": [...]}.
    `max_size_usd` é o teto desejado; pode retornar menor se book não dá.

    Retorna DepthQuote com fillable_size_usd <= max_size_usd, sempre
    respeitando o cap de impacto.
    """
    asks = book.get("asks") or []
    if not asks:
        return DepthQuote(
            fillable_size_usd=0.0, vwap_price=0.0, levels_consumed=0,
            best_price=0.0, impact_pct=0.0, book_thin=True,
        )
    parsed = [(float(a["price"]), float(a["size"])) for a in asks[:max_levels]]
    parsed.sort(key=lambda x: x[0])  # ascending
    best = parsed[0][0]
    impact_cap_price = best * (1 + max_impact_pct)

    spent_usd = 0.0
    tokens = 0.0
    levels_used = 0
    for price, size in parsed:
        if price > impact_cap_price:
            break
        # Quanto $ disponível ainda cabe no max_size_usd?
        remaining_usd = max_size_usd - spent_usd
        if remaining_usd <= 0:
            break
        # Quanto desse nível conseguimos consumir?
        level_capacity_usd = price * size
        take_usd = min(level_capacity_usd, remaining_usd)
        spent_usd += take_usd
        tokens += take_usd / price
        levels_used += 1

    if tokens <= 0:
        return DepthQuote(
            fillable_size_usd=0.0, vwap_price=best, levels_consumed=0,
            best_price=best, impact_pct=0.0, book_thin=True,
        )
    vwap = spent_usd / tokens
    impact = (vwap - best) / best if best > 0 else 0.0
    return DepthQuote(
        fillable_size_usd=spent_usd,
        vwap_price=vwap,
        levels_consumed=levels_used,
        best_price=best,
        impact_pct=impact,
        book_thin=(levels_used < 2),
    )


def quote_sell_depth(
    book: dict[str, Any],
    max_size_usd: float,
    max_impact_pct: float,
    max_levels: int = 5,
) -> DepthQuote:
    """Simula um SELL consumindo bids até max_size_usd OU max_impact.

    Para SELL, impact é a queda do preço (best - vwap) / best — sempre positivo.
    """
    bids = book.get("bids") or []
    if not bids:
        return DepthQuote(
            fillable_size_usd=0.0, vwap_price=0.0, levels_consumed=0,
            best_price=0.0, impact_pct=0.0, book_thin=True,
        )
    parsed = [(float(b["price"]), float(b["size"])) for b in bids[:max_levels]]
    parsed.sort(key=lambda x: -x[0])  # descending (best bid first)
    best = parsed[0][0]
    # Para SELL, impact_cap é o piso de preço aceitável.
    impact_floor_price = best * (1 - max_impact_pct)

    spent_usd = 0.0
    tokens = 0.0
    levels_used = 0
    for price, size in parsed:
        if price < impact_floor_price:
            break
        remaining_usd = max_size_usd - spent_usd
        if remaining_usd <= 0:
            break
        level_capacity_usd = price * size
        take_usd = min(level_capacity_usd, remaining_usd)
        spent_usd += take_usd
        tokens += take_usd / price
        levels_used += 1

    if tokens <= 0:
        return DepthQuote(
            fillable_size_usd=0.0, vwap_price=best, levels_consumed=0,
            best_price=best, impact_pct=0.0, book_thin=True,
        )
    vwap = spent_usd / tokens
    impact = (best - vwap) / best if best > 0 else 0.0
    return DepthQuote(
        fillable_size_usd=spent_usd,
        vwap_price=vwap,
        levels_consumed=levels_used,
        best_price=best,
        impact_pct=impact,
        book_thin=(levels_used < 2),
    )
