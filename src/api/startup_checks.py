"""Startup health checks — rodados uma vez no amain() antes do loop.

Verifica:
  1. Geoblock: Polymarket bloqueia IPs dos EUA para exchange internacional.
     GET https://polymarket.com/api/geoblock → { blocked: bool, country, ip }
  2. Latency baseline: RTT para clob.polymarket.com + data-api.
     Persiste em `latency_probes` para auditoria.

Decisão de fail-fast fica com o caller (main.py).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import orjson

from src.api.http import get_http_client
from src.core.database import DEFAULT_DB_PATH, get_connection
from src.core.logger import get_logger

log = get_logger(__name__)

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
LATENCY_TARGETS = {
    "clob": "https://clob.polymarket.com/",
    "data_api": "https://data-api.polymarket.com/",
    "gamma": "https://gamma-api.polymarket.com/",
}


async def check_geoblock() -> dict[str, Any]:
    """Retorna `{blocked, country, ip}` ou `{blocked: None, error: ...}`."""
    client = await get_http_client()
    try:
        resp = await client.get(GEOBLOCK_URL, timeout=10.0)
        resp.raise_for_status()
        data = orjson.loads(resp.content)
        log.info(
            "geoblock_check",
            blocked=data.get("blocked"),
            country=data.get("country"),
            ip=data.get("ip"),
        )
        return data
    except httpx.HTTPError as e:
        log.warning("geoblock_check_failed", err=repr(e))
        return {"blocked": None, "error": repr(e)}


async def measure_rtt(url: str) -> tuple[float | None, int | None, str | None]:
    """Retorna (rtt_ms, status_code, error_str). Faz GET, não POST."""
    client = await get_http_client()
    t0 = time.perf_counter()
    try:
        resp = await client.get(url, timeout=5.0)
        rtt_ms = (time.perf_counter() - t0) * 1000.0
        return rtt_ms, resp.status_code, None
    except httpx.HTTPError as e:
        return None, None, repr(e)


async def latency_baseline(
    *, db_path: Path = DEFAULT_DB_PATH, vps_location: str = "quantvps-ny",
) -> dict[str, float | None]:
    """Mede RTT para cada target e persiste em latency_probes."""
    now = datetime.now(timezone.utc).isoformat()
    results: dict[str, float | None] = {}

    async with get_connection(db_path) as db:
        for name, url in LATENCY_TARGETS.items():
            rtt, status, err = await measure_rtt(url)
            results[name] = rtt
            await db.execute(
                "INSERT INTO latency_probes "
                "(timestamp, target, rtt_ms, status_code, error, vps_location) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now, name, rtt if rtt is not None else 0.0, status, err, vps_location),
            )
            log.info("latency_probe", target=name, rtt_ms=rtt, status=status)
        await db.commit()

    return results
