"""H3 — FOK fallback para GTC que não preenche em N segundos.

Fluxo: após `post_order` (GTC), agendamos um watchdog async. Se depois
de `fok_fallback_timeout_seconds` a ordem ainda não fechou, cancelamos
e reenviamos como FOK (Fill-Or-Kill) ao melhor preço disponível.

A verificação do status usa o SDK síncrono via run_in_executor.
"""
from __future__ import annotations

import asyncio
from typing import Any

from src.api.clob_client import CLOBClient
from src.api.order_builder import OrderDraft
from src.core.exceptions import PolymarketAPIError
from src.core.logger import get_logger

log = get_logger(__name__)


def _is_terminal(status: str) -> bool:
    """Filled/cancelled/rejected = não precisa mais watchdog."""
    return status.lower() in ("matched", "filled", "cancelled", "canceled", "rejected")


async def watchdog_order(
    *,
    clob: CLOBClient,
    order_id: str,
    draft: OrderDraft,
    timeout_s: int = 30,
    poll_interval_s: float = 2.0,
) -> dict[str, Any] | None:
    """Monitora ordem GTC. Se timeout, cancela + reenvia FOK.

    Returns:
        dict do novo post_order FOK se fallback disparou, None se a
        original preencheu dentro do timeout.
    """
    if order_id == "":
        return None
    signer = clob._pick_signer(draft)
    loop = asyncio.get_running_loop()
    deadline = asyncio.get_event_loop().time() + timeout_s

    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(poll_interval_s)
        try:
            status_resp: dict[str, Any] = await loop.run_in_executor(
                None, lambda: signer.get_order(order_id),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("watchdog_status_check_failed", order_id=order_id, err=repr(exc))
            continue
        status = str(status_resp.get("status") or "").lower()
        if _is_terminal(status):
            log.info("watchdog_order_terminal", order_id=order_id, status=status)
            return None

    log.warning("watchdog_timeout_firing_fok_fallback", order_id=order_id)
    # 1. Cancela original
    try:
        await loop.run_in_executor(None, lambda: signer.cancel(order_id))
    except Exception as exc:  # noqa: BLE001
        log.warning("watchdog_cancel_failed", err=repr(exc))

    # 2. Repost como FOK (retorna dict ou levanta PolymarketAPIError)
    try:
        return await clob.post_order(draft, order_type="FOK")
    except PolymarketAPIError as exc:
        log.error("fok_fallback_also_failed", err=repr(exc))
        return None
