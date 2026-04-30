"""ARQUITETURA — BackgroundRateLimiter.

Garante que auditoria (Scanner enrich, resolution batch, snapshot_whale)
nunca compita com hot path por slots de conexão TCP.
"""
from __future__ import annotations

import asyncio

import pytest

from src.api.background_limiter import (
    BackgroundRateLimiter,
    configure_background_limiter,
    get_background_limiter,
)


@pytest.mark.asyncio
async def test_limiter_caps_concurrent_acquires():
    """Cap=2 → no máximo 2 in-flight simultaneamente."""
    bg = BackgroundRateLimiter(max_concurrent=2)
    peak = 0
    in_flight = 0
    lock = asyncio.Lock()

    async def task(i: int) -> None:
        nonlocal peak, in_flight
        async with bg.acquire():
            async with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            await asyncio.sleep(0.02)
            async with lock:
                in_flight -= 1

    tasks = [asyncio.create_task(task(i)) for i in range(10)]
    await asyncio.gather(*tasks)
    assert peak == 2  # nunca passou do cap


@pytest.mark.asyncio
async def test_limiter_in_flight_metric():
    """`in_flight` reflete slots usados."""
    bg = BackgroundRateLimiter(max_concurrent=3)
    assert bg.in_flight == 0

    started = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        async with bg.acquire():
            started.set()
            await release.wait()

    t = asyncio.create_task(hold())
    await started.wait()
    assert bg.in_flight == 1
    release.set()
    await t
    assert bg.in_flight == 0


@pytest.mark.asyncio
async def test_singleton_returns_same_instance():
    a = get_background_limiter()
    b = get_background_limiter()
    assert a is b


@pytest.mark.asyncio
async def test_configure_replaces_singleton():
    """configure_background_limiter substitui instância global."""
    new = configure_background_limiter(max_concurrent=10)
    assert get_background_limiter() is new
    assert new.max_concurrent == 10


@pytest.mark.asyncio
async def test_acquire_releases_on_exception():
    """Exceção dentro do `async with` libera slot (GC pattern)."""
    bg = BackgroundRateLimiter(max_concurrent=1)

    with pytest.raises(ValueError):
        async with bg.acquire():
            assert bg.in_flight == 1
            raise ValueError("boom")
    assert bg.in_flight == 0  # liberado mesmo com raise


@pytest.mark.asyncio
async def test_limiter_serializes_when_full():
    """Cap=1 com 4 tasks → execução serializada."""
    bg = BackgroundRateLimiter(max_concurrent=1)
    order: list[int] = []

    async def task(i: int) -> None:
        async with bg.acquire():
            order.append(i)
            await asyncio.sleep(0.01)

    await asyncio.gather(*[task(i) for i in range(4)])
    # FIFO order esperada (Semaphore acquire é fair).
    assert order == [0, 1, 2, 3]
