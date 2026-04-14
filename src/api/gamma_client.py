"""Polymarket Gamma API — metadados de mercado com cache.

Responsabilidades:
  1. Buscar mercado por condition_id (`end_date_iso`, `expiration`, tokens).
  2. Extrair `tick_size` (Diretiva 1) e `neg_risk` (Diretiva 4).
  3. Gravar em market_metadata_cache com TTL (padrão 300s).

O cache é fundamental: o Tracker chama gamma em cada sinal para aplicar
o filtro de duração de mercado. Sem cache, 1 trade/seg = 3600 chamadas/h.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import orjson

from src.api.http import get_http_client
from src.core.database import DEFAULT_DB_PATH, get_connection
from src.core.exceptions import PolymarketAPIError, RateLimitError
from src.core.logger import get_logger

log = get_logger(__name__)

DEFAULT_BASE_URL = "https://gamma-api.polymarket.com"
DEFAULT_TTL_SECONDS = 300


class GammaAPIClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        db_path: Path = DEFAULT_DB_PATH,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._db_path = db_path
        self._ttl = ttl_seconds

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        client = await get_http_client()
        try:
            resp = await client.get(f"{self._base}{path}", params=params)
        except httpx.HTTPError as e:
            raise PolymarketAPIError(f"gamma network error: {e}", endpoint=path) from e
        if resp.status_code == 429:
            raise RateLimitError(
                "gamma rate limit", endpoint=path, status=429,
                retry_after=float(resp.headers.get("retry-after", "1")),
            )
        if resp.status_code >= 400:
            raise PolymarketAPIError(
                f"gamma {resp.status_code}",
                endpoint=path, status=resp.status_code, detail=resp.text[:500],
            )
        return orjson.loads(resp.content)

    async def _read_cache(self, condition_id: str) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        async with get_connection(self._db_path) as db:
            async with db.execute(
                "SELECT title, slug, end_date, expiration, active, closed, resolved, "
                "tokens_json, fetched_at, ttl_seconds, tick_size, neg_risk "
                "FROM market_metadata_cache WHERE condition_id = ?",
                (condition_id,),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        fetched_at = datetime.fromisoformat(row["fetched_at"])
        age = (now - fetched_at).total_seconds()
        if age > row["ttl_seconds"]:
            return None
        return {
            "condition_id": condition_id,
            "title": row["title"],
            "slug": row["slug"],
            "end_date": row["end_date"],
            "expiration": row["expiration"],
            "active": bool(row["active"]) if row["active"] is not None else None,
            "closed": bool(row["closed"]) if row["closed"] is not None else None,
            "resolved": bool(row["resolved"]) if row["resolved"] is not None else None,
            "tokens_json": row["tokens_json"],
            "tick_size": row["tick_size"],
            "neg_risk": bool(row["neg_risk"]),
            "_cached": True,
        }

    async def _write_cache(self, condition_id: str, market: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        tokens_json = orjson.dumps(market.get("tokens", [])).decode()
        tick_size = market.get("minimum_tick_size") or market.get("tick_size") or 0.01
        neg_risk = 1 if market.get("neg_risk") or market.get("negRisk") else 0
        async with get_connection(self._db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO market_metadata_cache "
                "(condition_id, title, slug, end_date, expiration, active, closed, "
                " resolved, tokens_json, fetched_at, ttl_seconds, tick_size, neg_risk) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    condition_id,
                    market.get("question") or market.get("title"),
                    market.get("slug"),
                    (
                        market.get("end_date_iso")
                        or market.get("end_date")
                        or market.get("endDate")  # Gamma 2026: camelCase
                    ),
                    market.get("expiration"),
                    int(bool(market.get("active"))) if market.get("active") is not None else None,
                    int(bool(market.get("closed"))) if market.get("closed") is not None else None,
                    int(bool(market.get("resolved"))) if market.get("resolved") is not None else None,
                    tokens_json,
                    now,
                    self._ttl,
                    float(tick_size),
                    neg_risk,
                ),
            )
            await db.commit()

    async def get_market(self, condition_id: str, *, force_refresh: bool = False) -> dict[str, Any]:
        if not force_refresh:
            cached = await self._read_cache(condition_id)
            if cached:
                return cached
        # Gamma 2026: filter é `condition_ids` (plural); `condition_id` é ignorado.
        data = await self._get("/markets", params={"condition_ids": condition_id})
        market: dict[str, Any] | None = None
        if isinstance(data, list):
            # Confere cid retornado — em caso raro o filtro falha silenciosamente.
            for m in data:
                if (m.get("conditionId") or "").lower() == condition_id.lower():
                    market = m
                    break
            if market is None and data:
                # Fallback: primeiro resultado (se a API mudar comportamento)
                market = data[0]
        elif isinstance(data, dict):
            market = data
        if market is None:
            raise PolymarketAPIError(
                f"mercado não encontrado: {condition_id}",
                endpoint="/markets", status=404,
            )
        await self._write_cache(condition_id, market)
        log.info(
            "gamma_market_fetched",
            condition_id=condition_id,
            tick_size=market.get("minimum_tick_size") or market.get("tick_size"),
            neg_risk=bool(market.get("neg_risk") or market.get("negRisk")),
        )
        return market
