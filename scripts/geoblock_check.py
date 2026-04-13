"""Verifica se o IP corrente está bloqueado pela Polymarket.

Uso:
    uv run python scripts/geoblock_check.py

Exit codes:
    0 — não bloqueado
    1 — bloqueado (stdout detalha país e IP)
    2 — erro de rede ao consultar
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api.http import close_http_client
from src.api.startup_checks import check_geoblock


async def main() -> int:
    result = await check_geoblock()
    await close_http_client()

    if result.get("error"):
        print(f"ERROR: {result['error']}")
        return 2
    if result.get("blocked"):
        print(f"BLOCKED — country={result.get('country')} ip={result.get('ip')}")
        return 1
    print(f"OK — country={result.get('country')} ip={result.get('ip')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
