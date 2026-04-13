"""Diretiva 2 — backoff exponencial para HTTP 425."""
from unittest.mock import MagicMock

import httpx
import pytest

from src.api.retry import MatchingEngineRestartError, retry_on_425


def _make_425() -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://clob.polymarket.com/order")
    resp = httpx.Response(425, request=req)
    return httpx.HTTPStatusError("Too Early", request=req, response=resp)


def _make_500() -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://clob.polymarket.com/order")
    resp = httpx.Response(500, request=req)
    return httpx.HTTPStatusError("Server Error", request=req, response=resp)


async def test_retries_on_425_then_succeeds():
    calls = MagicMock()
    state = {"fails_left": 2}

    @retry_on_425(initial_delay=0.001, max_delay=0.002, max_attempts=5)
    async def op():
        calls()
        if state["fails_left"]:
            state["fails_left"] -= 1
            raise _make_425()
        return "ok"

    assert await op() == "ok"
    assert calls.call_count == 3


async def test_raises_after_max_attempts():
    @retry_on_425(initial_delay=0.001, max_delay=0.002, max_attempts=3)
    async def op():
        raise _make_425()

    with pytest.raises(MatchingEngineRestartError):
        await op()


async def test_non_425_propagates_immediately():
    calls = MagicMock()

    @retry_on_425(initial_delay=0.001, max_attempts=5)
    async def op():
        calls()
        raise _make_500()

    with pytest.raises(httpx.HTTPStatusError) as exc:
        await op()
    assert exc.value.response.status_code == 500
    assert calls.call_count == 1
