"""Diretiva 3 — watchdog de heartbeat L2."""
import asyncio

from src.api.heartbeat import HeartbeatWatchdog


async def test_failure_threshold_triggers_restore():
    state = {"hb_calls": 0, "restore_calls": 0}

    async def send_hb():
        state["hb_calls"] += 1
        raise RuntimeError("ws down")

    async def on_failure():
        state["restore_calls"] += 1

    wd = HeartbeatWatchdog(
        send_heartbeat=send_hb,
        on_failure=on_failure,
        interval_seconds=0.01,
        failure_threshold=3,
    )
    task = wd.start()
    await asyncio.sleep(0.08)
    await wd.stop()
    task  # already awaited via stop()

    assert state["restore_calls"] >= 1
    assert state["hb_calls"] >= 3


async def test_recovery_resets_counter():
    calls = {"hb": 0, "restore": 0}

    async def send_hb():
        calls["hb"] += 1
        if calls["hb"] <= 2:
            raise RuntimeError("boom")

    async def on_failure():
        calls["restore"] += 1

    wd = HeartbeatWatchdog(send_hb, on_failure, interval_seconds=0.01, failure_threshold=5)
    wd.start()
    await asyncio.sleep(0.06)
    await wd.stop()

    # 2 falhas < threshold(5) → sem restore; e depois heartbeat volta.
    assert calls["restore"] == 0
    assert calls["hb"] >= 3
