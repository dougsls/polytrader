"""Backpressure: queue full → drop mais antigo, incrementa metrics."""
import asyncio
from datetime import datetime, timezone

import pytest

from src.core import metrics
from src.core.models import TradeSignal


def _sig(sid: str) -> TradeSignal:
    return TradeSignal.model_construct(
        id=sid, wallet_address="0xW", wallet_score=0.8,
        condition_id="c", token_id="t", side="BUY",
        size=10.0, price=0.5, usd_value=5.0,
        market_title="m", outcome="Yes",
        detected_at=datetime.now(timezone.utc),
        source="websocket", status="pending",
        skip_reason=None, market_end_date=None,
        hours_to_resolution=None,
    )


async def test_put_nowait_does_not_block_full_queue():
    """Emula o hot path do trade_monitor com queue saturada."""
    q: asyncio.Queue = asyncio.Queue(maxsize=3)
    for i in range(3):
        q.put_nowait(_sig(f"old-{i}"))
    assert q.full()

    before_dropped = metrics.signals_dropped._value.get()

    new_sig = _sig("new-1")
    try:
        q.put_nowait(new_sig)
        pytest.fail("deveria ter levantado QueueFull")
    except asyncio.QueueFull:
        dropped = q.get_nowait()
        q.task_done()
        q.put_nowait(new_sig)
        metrics.signals_dropped.inc()

    assert dropped.id == "old-0"  # FIFO: dropa o mais antigo
    assert q.qsize() == 3
    # Último item = o novo
    seen = []
    while not q.empty():
        seen.append(q.get_nowait().id)
        q.task_done()
    assert seen[-1] == "new-1"

    after_dropped = metrics.signals_dropped._value.get()
    assert after_dropped == before_dropped + 1
