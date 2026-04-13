"""Mede RTT NY → London (Polymarket CLOB + Data + Gamma).

Roda 10 probes consecutivos por target, imprime p50/p95/min/max.
Uso: uv run python scripts/latency_test.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api.http import close_http_client
from src.api.startup_checks import LATENCY_TARGETS, measure_rtt

PROBES = 10


def _p95(xs: list[float]) -> float:
    s = sorted(xs)
    i = max(0, int(len(s) * 0.95) - 1)
    return s[i]


async def main() -> int:
    print(f"Running {PROBES} probes per target…\n")
    for name, url in LATENCY_TARGETS.items():
        samples: list[float] = []
        for _ in range(PROBES):
            rtt, _status, err = await measure_rtt(url)
            if rtt is not None:
                samples.append(rtt)
            elif err:
                print(f"  {name}: error {err}")
        if samples:
            print(
                f"{name:<10} min={min(samples):.1f}ms  p50={median(samples):.1f}ms  "
                f"p95={_p95(samples):.1f}ms  max={max(samples):.1f}ms  n={len(samples)}"
            )
        else:
            print(f"{name:<10} NO DATA")

    await close_http_client()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
