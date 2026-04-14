"""H6 — Exposure bucketing por tag de mercado.

Dois mercados com tag comum (ex: ambos "politics" ou ambos "election-2028")
são tratados como correlacionados. Se o portfolio já tem > max_tag_exposure_pct
concentrado numa tag, novo BUY na mesma tag é rejeitado.

Consome o campo `tags` ou `category` da Gamma API (cacheado em
`market_metadata_cache.tokens_json` — extraímos em runtime).
"""
from __future__ import annotations

from typing import Any

import aiosqlite
import orjson

from src.core.logger import get_logger

log = get_logger(__name__)


def extract_tags(market: dict[str, Any]) -> list[str]:
    """Normaliza tags da Gamma API. Aceita variações do formato."""
    raw = (
        market.get("tags")
        or market.get("category")
        or market.get("categories")
        or []
    )
    if isinstance(raw, str):
        return [raw.lower().strip()]
    if isinstance(raw, list):
        return [str(t).lower().strip() for t in raw if t]
    return []


async def compute_tag_exposure(
    conn: aiosqlite.Connection,
) -> dict[str, float]:
    """Agrega USD investido por tag a partir de posições abertas.

    Lê `tokens_json` em market_metadata_cache (onde persistimos tags).
    Retorna `{tag: invested_usd}`.
    """
    async with conn.execute(
        "SELECT bp.condition_id, bp.size * bp.avg_entry_price AS invested, "
        "       m.tokens_json "
        "FROM bot_positions bp "
        "LEFT JOIN market_metadata_cache m ON m.condition_id = bp.condition_id "
        "WHERE bp.is_open=1"
    ) as cur:
        rows = await cur.fetchall()

    exposure: dict[str, float] = {}
    for cid, invested, tokens_json in rows:
        tags: list[str] = []
        if tokens_json:
            try:
                parsed = orjson.loads(tokens_json)
                if isinstance(parsed, dict):
                    tags = extract_tags(parsed)
            except orjson.JSONDecodeError:
                pass
        if not tags:
            tags = ["_untagged"]
        for t in tags:
            exposure[t] = exposure.get(t, 0.0) + float(invested or 0.0)
    return exposure


async def would_breach_tag_cap(
    *,
    conn: aiosqlite.Connection,
    market: dict[str, Any],
    new_usd: float,
    portfolio_value: float,
    max_tag_exposure_pct: float,
) -> tuple[bool, str | None, float]:
    """Checa se o trade proposto estoura o cap por tag.

    Returns (breach, tag_that_breached, pct).
    """
    if portfolio_value <= 0:
        return False, None, 0.0
    new_tags = extract_tags(market)
    if not new_tags:
        return False, None, 0.0
    current = await compute_tag_exposure(conn)
    for tag in new_tags:
        projected = current.get(tag, 0.0) + new_usd
        pct = projected / portfolio_value
        if pct > max_tag_exposure_pct:
            return True, tag, pct
    return False, None, 0.0
