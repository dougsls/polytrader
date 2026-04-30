"""Polymarket Gamma API — metadados de mercado, cache 3-tier.

HFT — pirâmide de latências:
    L1: RAM (OrderedDict, ~50ns hit)        → hot path
    L2: SQLite WAL (~500µs hit, durável)    → warm path / restart
    L3: REST Gamma API (~80-130ms NY→London) → cold path

`get_market` desce essa pirâmide em sequência: RAM → SQLite → REST.
Cada miss faz write-through pra camada acima. O resultado: o
`detect_signal` (chamado a cada trade do RTDS) faz lookup O(1) na
RAM em ~99% dos casos depois do warm-up.

Responsabilidades:
  1. Buscar mercado por condition_id (`end_date_iso`, `expiration`, tokens).
  2. Extrair `tick_size` (Diretiva 1) e `neg_risk` (Diretiva 4).
  3. Cachear em RAM + SQLite com TTL (padrão 300s).
"""
from __future__ import annotations

import time
from collections import OrderedDict
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
# Cap do RAM cache. Polymarket raramente tem >500 mercados ativos
# simultâneos; 1024 dá folga de 2x sem virar memory hog (cada entry
# ~2KB = ~2MB total no pior caso).
RAM_CACHE_MAX_SIZE = 1024


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
        # HFT — RAM cache (L1). Chave = condition_id, valor = (market_dict,
        # monotonic_fetched_at). monotonic é safe contra ajuste de relógio.
        # OrderedDict permite eviction LRU O(1) via popitem(last=False).
        self._ram_cache: OrderedDict[str, tuple[dict[str, Any], float]] = OrderedDict()
        # Métricas internas (low-overhead, lidas pelo dashboard se quiser).
        self._ram_hits = 0
        self._ram_misses = 0

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

    # --- RAM cache (L1) — O(1) lookup, microsegundos -------------------- #

    def _ram_get(self, condition_id: str) -> dict[str, Any] | None:
        """RAM hit ou None. NUNCA toca disco. Síncrono, sub-µs."""
        entry = self._ram_cache.get(condition_id)
        if entry is None:
            self._ram_misses += 1
            return None
        market, fetched_at = entry
        if (time.monotonic() - fetched_at) > self._ttl:
            # TTL expirou — drop e força revalidação.
            self._ram_cache.pop(condition_id, None)
            self._ram_misses += 1
            return None
        # LRU: move pro fim (mais recente). O(1) na OrderedDict.
        self._ram_cache.move_to_end(condition_id)
        self._ram_hits += 1
        return market

    def _ram_set(self, condition_id: str, market: dict[str, Any]) -> None:
        """Escreve em RAM com eviction LRU se cap atingido."""
        self._ram_cache[condition_id] = (market, time.monotonic())
        self._ram_cache.move_to_end(condition_id)
        # Eviction quando passa do cap — drop o mais antigo (front).
        while len(self._ram_cache) > RAM_CACHE_MAX_SIZE:
            self._ram_cache.popitem(last=False)

    @property
    def ram_stats(self) -> dict[str, int]:
        """Hits/misses para o dashboard. Total = hits + misses."""
        return {
            "hits": self._ram_hits,
            "misses": self._ram_misses,
            "size": len(self._ram_cache),
            "max_size": RAM_CACHE_MAX_SIZE,
        }

    # --- SQLite cache (L2) — warm path, persiste reboots --------------- #

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
            "tokens": orjson.loads(row["tokens_json"]) if row["tokens_json"] else [],
            "tick_size": row["tick_size"],
            "neg_risk": bool(row["neg_risk"]),
            "_cached": True,
        }

    @staticmethod
    def _normalize_tokens(market: dict[str, Any]) -> list[dict[str, Any]]:
        """Gamma 2026 retorna outcomes/clobTokenIds como JSON-strings paralelas;
        signal_detector + order_manager esperam shape CLOB: [{token_id, outcome}].
        """
        existing = market.get("tokens")
        if isinstance(existing, list) and existing:
            return existing
        outs = market.get("outcomes")
        ids = market.get("clobTokenIds")
        if isinstance(outs, str):
            try:
                outs = orjson.loads(outs)
            except Exception:
                outs = None
        if isinstance(ids, str):
            try:
                ids = orjson.loads(ids)
            except Exception:
                ids = None
        if not isinstance(outs, list) or not isinstance(ids, list):
            return []
        return [
            {"token_id": str(tid), "outcome": str(name)}
            for tid, name in zip(ids, outs, strict=False)
        ]

    async def _write_cache(self, condition_id: str, market: dict[str, Any]) -> None:
        # Mirror em RAM ANTES do disco — hot path tem o dado mesmo se
        # o INSERT atrasar (ex: WAL contention sob load).
        self._ram_set(condition_id, market)
        now = datetime.now(timezone.utc).isoformat()
        market["tokens"] = self._normalize_tokens(market)
        tokens_json = orjson.dumps(market["tokens"]).decode()
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

    async def list_active_markets(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        max_hours_to_resolution: float | None = None,
        min_minutes_to_resolution: float | None = None,
    ) -> list[dict[str, Any]]:
        """Lista mercados ativos paginados, normalizando tokens para shape CLOB.

        Filtra no cliente:
          - active=True, closed=False, archived=False
          - end_date dentro de janela [min_minutes, max_hours]
        Usado pelo arbitrage scanner para varrer book de YES/NO em massa.

        Não-cacheado: o status (active/closed/end_date) muda dinamicamente
        e não pode confiar no cache de TTL=300s do market_metadata_cache.
        """
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "active": "true",
            "closed": "false",
            "archived": "false",
        }
        data = await self._get("/markets", params=params)
        if not isinstance(data, list):
            return []
        now = datetime.now(timezone.utc)
        out: list[dict[str, Any]] = []
        for m in data:
            m["tokens"] = self._normalize_tokens(m)
            if not m["tokens"] or len(m["tokens"]) < 2:
                continue
            end_iso = m.get("end_date_iso") or m.get("endDate") or m.get("end_date")
            hours = None
            if end_iso:
                try:
                    end_dt = datetime.fromisoformat(str(end_iso).replace("Z", "+00:00"))
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    hours = (end_dt - now).total_seconds() / 3600.0
                except ValueError:
                    hours = None
            m["_hours_to_resolution"] = hours
            if max_hours_to_resolution is not None and hours is not None:
                if hours > max_hours_to_resolution:
                    continue
            if min_minutes_to_resolution is not None and hours is not None:
                if hours * 60 < min_minutes_to_resolution:
                    continue
            out.append(m)
        return out

    async def get_markets_batch(
        self,
        condition_ids: list[str],
        *,
        chunk_size: int = 50,
    ) -> dict[str, dict[str, Any]]:
        """Batch fetch — 1 request HTTP por chunk em vez de N.

        ⚠️ RATE-LIMIT FIX: o resolution_watcher antigo fazia N requests
        sequenciais (1 por position aberta). Em 50 positions × tick
        de 60s = 50 reqs/min bypassando cache → ban Cloudflare.

        Esta função:
            1. Particiona em chunks ≤ 50 (URL length limit ~3KB).
            2. Faz UMA request /markets?condition_ids=ID1,ID2,...
            3. Mira RAM + SQLite cache pra cada market retornado.
            4. Retorna dict {condition_id_lower → market_dict}.

        Returns:
            Dict com chave em LOWERCASE; condition_ids missing no
            response são silenciosamente omitidos (caller decide o que
            fazer com a ausência).
        """
        if not condition_ids:
            return {}
        # Dedup case-insensitive — Gamma trata IDs case-insensitive mas
        # nosso cache usa o case original.
        unique_ids = list({cid for cid in condition_ids if cid})
        out: dict[str, dict[str, Any]] = {}

        for i in range(0, len(unique_ids), chunk_size):
            chunk = unique_ids[i : i + chunk_size]
            # Polymarket Gamma 2026: aceita lista comma-separated.
            params = {"condition_ids": ",".join(chunk)}
            try:
                data = await self._get("/markets", params=params)
            except PolymarketAPIError as exc:
                log.warning(
                    "gamma_batch_chunk_failed",
                    chunk_start=i, chunk_size=len(chunk), err=repr(exc)[:80],
                )
                continue
            if not isinstance(data, list):
                continue
            for market in data:
                cid = market.get("conditionId") or market.get("condition_id")
                if not cid:
                    continue
                # Mira ambos os caches pra acelerar próximas leituras.
                await self._write_cache(cid, market)
                out[cid.lower()] = market
        return out

    async def get_market(self, condition_id: str, *, force_refresh: bool = False) -> dict[str, Any]:
        # HFT — pirâmide 3-tier: RAM (~50ns) → SQLite (~500µs) → REST (~100ms).
        if not force_refresh:
            # L1 — RAM hot path. Nenhum I/O, sub-microsegundo.
            ram_hit = self._ram_get(condition_id)
            if ram_hit is not None:
                return ram_hit
            # L2 — SQLite warm path. Sobrevive a restart do processo.
            cached = await self._read_cache(condition_id)
            if cached:
                # Promove pra RAM — próximas chamadas serão L1 hits.
                self._ram_set(condition_id, cached)
                return cached
        # L3 — REST cold path.
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
