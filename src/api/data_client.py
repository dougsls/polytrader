"""Polymarket Data API — leaderboard, positions, trades, activity.

Público, sem auth. Usado pelo Scanner (leaderboard + performance) e pelo
Tracker (polling de trades + /positions para montar whale_inventory).

Todos os retornos são dicts Python (orjson parse). Modelos Pydantic do
domínio (TrackedWallet, TradeSignal) são construídos no chamador — este
client entrega apenas o payload cru normalizado.
"""
from __future__ import annotations

from typing import Any

import httpx
import orjson

from src.api.http import get_http_client
from src.core.exceptions import PolymarketAPIError, RateLimitError
from src.core.logger import get_logger

log = get_logger(__name__)

DEFAULT_BASE_URL = "https://data-api.polymarket.com"


class DataAPIClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL) -> None:
        self._base = base_url.rstrip("/")

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        client = await get_http_client()
        url = f"{self._base}{path}"
        try:
            resp = await client.get(url, params=params)
        except httpx.HTTPError as e:
            raise PolymarketAPIError(
                f"data-api network error: {e}", endpoint=path
            ) from e
        if resp.status_code == 429:
            retry = float(resp.headers.get("retry-after", "1"))
            raise RateLimitError(
                "data-api rate limit", endpoint=path, status=429, retry_after=retry
            )
        if resp.status_code >= 400:
            raise PolymarketAPIError(
                f"data-api {resp.status_code}",
                endpoint=path,
                status=resp.status_code,
                detail=resp.text[:500],
            )
        return orjson.loads(resp.content)

    # --- Leaderboard ------------------------------------------------------

    async def leaderboard(
        self,
        period: str = "7d",
        order_by: str = "pnl",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        data = await self._get(
            "/leaderboard",
            params={"period": period, "orderBy": order_by, "limit": limit},
        )
        return data if isinstance(data, list) else data.get("data", [])

    # --- Carteira ---------------------------------------------------------

    async def positions(
        self,
        user: str,
        *,
        filter_type: str = "CASH",
        filter_amount: float = 1.0,
    ) -> list[dict[str, Any]]:
        data = await self._get(
            "/positions",
            params={"user": user, "filterType": filter_type, "filterAmount": filter_amount},
        )
        return data if isinstance(data, list) else []

    async def trades(
        self,
        user: str,
        *,
        start: int | None = None,
        end: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"user": user, "limit": limit}
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        data = await self._get("/trades", params=params)
        return data if isinstance(data, list) else []

    async def activity(
        self,
        user: str,
        *,
        type: str = "TRADE",
        side: str | None = None,
        start: int | None = None,
        end: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"user": user, "type": type}
        if side:
            params["side"] = side
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        data = await self._get("/activity", params=params)
        return data if isinstance(data, list) else []

    async def value(self, user: str) -> dict[str, Any]:
        return await self._get("/value", params={"user": user})
