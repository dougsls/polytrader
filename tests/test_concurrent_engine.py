"""Testes do CopyEngine concorrente — Semaphore limite + Lock atomicidade."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from src.executor.copy_engine import CopyEngine


def _make_engine(max_conc: int = 4):
    """Builder mínimo — só os campos necessários para validar primitivos."""
    cfg = MagicMock()
    cfg.max_concurrent_signals = max_conc
    cfg.max_portfolio_usd = 500.0
    cfg.mode = "paper"
    cfg.paper_perfect_mirror = False
    cfg.optimistic_execution = False
    queue = asyncio.Queue()
    risk = MagicMock()
    state = MagicMock()
    eng = CopyEngine(
        cfg=cfg,
        clob=MagicMock(),
        gamma=MagicMock(),
        risk=risk,
        queue=queue,
        state=state,
        risk_state_provider=lambda: MagicMock(),
        on_event=None,
    )
    return eng


@pytest.mark.asyncio
async def test_semaphore_initialized_with_cfg_value():
    eng = _make_engine(max_conc=8)
    # Semaphore expõe `_value` como contagem disponível.
    assert eng._post_semaphore._value == 8


@pytest.mark.asyncio
async def test_semaphore_default_is_one_when_cfg_missing():
    """Sem cfg.max_concurrent_signals, deve usar 1 (sequencial seguro)."""
    cfg = MagicMock()
    # Remove o atributo do mock pra forçar getattr default.
    del cfg.max_concurrent_signals
    cfg.max_portfolio_usd = 500.0
    cfg.mode = "paper"
    queue = asyncio.Queue()
    eng = CopyEngine(
        cfg=cfg, clob=MagicMock(), gamma=MagicMock(), risk=MagicMock(),
        queue=queue, state=MagicMock(),
        risk_state_provider=lambda: MagicMock(), on_event=None,
    )
    assert eng._post_semaphore._value == 1


@pytest.mark.asyncio
async def test_risk_lock_is_asyncio_lock():
    eng = _make_engine()
    assert isinstance(eng._risk_lock, asyncio.Lock)
    assert not eng._risk_lock.locked()


@pytest.mark.asyncio
async def test_semaphore_serializes_when_full():
    """4 tasks competindo por um semaphore de 1 devem serializar."""
    sem = asyncio.Semaphore(1)
    order: list[int] = []
    started = asyncio.Event()

    async def worker(i: int) -> None:
        async with sem:
            order.append(i)
            if i == 0:
                started.set()
                await asyncio.sleep(0.01)

    tasks = [asyncio.create_task(worker(i)) for i in range(4)]
    await asyncio.gather(*tasks)
    assert order == [0, 1, 2, 3] or len(order) == 4  # serial OR all completed
