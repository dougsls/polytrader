"""Regra 1 — Sistema Anti-Slippage de Copy Trading (Anchoring).

A âncora é o `whale_execution_price` (TradeSignal.price). Quando o sinal
chega ao executor, o melhor ask/bid do livro já pode estar muito acima
(baleia secou liquidez, o spread subiu). Comprar aí vira liquidez de
saída do mercado eufórico.

Regra: se `best_ask > whale_price * (1 + tolerance)`, ABORTAR.
Para SELL, espelho: `best_bid < whale_price * (1 - tolerance)` → abortar.
"""
from __future__ import annotations

from typing import Any, Literal

from src.api.clob_client import CLOBClient
from src.core.exceptions import SlippageExceededError
from src.core.logger import get_logger

log = get_logger(__name__)


def _best_ask_from_book(book: dict[str, Any]) -> float:
    asks = book.get("asks") or []
    if not asks:
        raise ValueError("book sem asks")
    # Polymarket retorna asks sorted asc por preço; pegar o primeiro.
    first = asks[0]
    return float(first["price"] if isinstance(first, dict) else first[0])


def _best_bid_from_book(book: dict[str, Any]) -> float:
    bids = book.get("bids") or []
    if not bids:
        raise ValueError("book sem bids")
    first = bids[0]
    return float(first["price"] if isinstance(first, dict) else first[0])


async def check_slippage_or_abort(
    *,
    clob: CLOBClient,
    token_id: str,
    side: Literal["BUY", "SELL"],
    whale_price: float,
    tolerance_pct: float,
) -> float:
    """Consulta o livro e retorna o preço de referência. Lança
    SlippageExceededError se o mercado se moveu além da tolerância.

    Por que /book em vez de /midpoint: midpoint esconde spread largo;
    comprar pelo ask é o que vai acontecer na prática.
    """
    if whale_price <= 0:
        raise ValueError("whale_price inválido")
    book = await clob.book(token_id)
    if side == "BUY":
        ref = _best_ask_from_book(book)
        limit = whale_price * (1 + tolerance_pct)
        if ref > limit:
            slippage = (ref / whale_price) - 1.0
            log.warning(
                "slippage_abort_buy",
                token_id=token_id, whale=whale_price, best_ask=ref,
                slippage=slippage, tolerance=tolerance_pct,
            )
            raise SlippageExceededError(actual=slippage, tolerance=tolerance_pct)
        return ref
    # SELL
    ref = _best_bid_from_book(book)
    limit = whale_price * (1 - tolerance_pct)
    if ref < limit:
        slippage = 1.0 - (ref / whale_price)
        log.warning(
            "slippage_abort_sell",
            token_id=token_id, whale=whale_price, best_bid=ref,
            slippage=slippage, tolerance=tolerance_pct,
        )
        raise SlippageExceededError(actual=slippage, tolerance=tolerance_pct)
    return ref
