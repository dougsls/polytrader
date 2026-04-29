"""Regra 1 — Sistema Anti-Slippage de Copy Trading (Anchoring).

A âncora é o `whale_execution_price` (TradeSignal.price). Quando o sinal
chega ao executor, o melhor ask/bid do livro já pode estar muito acima
(baleia secou liquidez, o spread subiu). Comprar aí vira liquidez de
saída do mercado eufórico.

Regra: se `best_ask > whale_price * (1 + tolerance)`, ABORTAR.
Para SELL, espelho: `best_bid < whale_price * (1 - tolerance)` → abortar.

Dois modos de aplicação da regra:
  - **Defensive** (`check_slippage_or_abort`): puxa /book antes de postar.
    Aborta no client. 1 round-trip extra (~80-130ms NY→London).
  - **Optimistic** (`compute_optimistic_ref_price`): pura, sem I/O.
    Embute a tolerance no limit price e deixa o CLOB rejeitar via FOK
    se o livro andou. Zero round-trip extra; trade-off HFT clássico.
"""
from __future__ import annotations

from typing import Any, Literal

from src.api.clob_client import CLOBClient
from src.core.exceptions import SlippageExceededError, SpreadTooWideError
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


def compute_optimistic_ref_price(
    *,
    side: Literal["BUY", "SELL"],
    whale_price: float,
    tolerance_pct: float,
) -> float:
    """Optimistic Execution — calcula o limit price sem chamar /book.

    A âncora continua sendo o `whale_price`. A tolerância é embutida no
    limit price: BUY paga até `whale_price × (1 + tolerance)`, SELL
    aceita receber até `whale_price × (1 - tolerance)`. O CLOB rejeita
    on-exchange (FOK) se o book andou além desse limite — economizamos
    o round-trip REST de /book.

    Trade-off vs check_slippage_or_abort:
      - PRO: 1 round-trip a menos (~80-130ms NY→London poupados)
      - PRO: a decisão de match acontece no matching engine, não no client
      - CON: ordem chega ao CLOB e é rejeitada se livro andou; isso conta
              como "trade falhado" mas não como exposure
      - CON: sem spread shield (deve ser feito separadamente se desejado)

    Use combinado com `default_order_type: "FOK"` para garantir que
    rejeições não deixem GTC pendurado.
    """
    if whale_price <= 0:
        raise ValueError("whale_price inválido")
    if side == "BUY":
        return whale_price * (1.0 + tolerance_pct)
    return whale_price * (1.0 - tolerance_pct)


async def check_slippage_or_abort(
    *,
    clob: CLOBClient,
    token_id: str,
    side: Literal["BUY", "SELL"],
    whale_price: float,
    tolerance_pct: float,
    max_spread: float | None = None,
) -> float:
    """Dupla trava pré-submissão. Retorna o preço de referência ou lança.

    Travas aplicadas nesta ordem:
      1. **Spread shield** (se `max_spread` fornecido): se ask-bid é
         maior que o limite, o pool está ilíquido e o preenchimento
         vai dreno o capital. `SpreadTooWideError`.
      2. **Regra 1 (Anti-Slippage Anchoring)**: compara best_ask/bid
         contra whale_price ± tolerance_pct. `SlippageExceededError`.

    Por que /book em vez de /midpoint: midpoint esconde spread largo;
    comprar pelo ask é o que vai acontecer na prática.
    """
    if whale_price <= 0:
        raise ValueError("whale_price inválido")
    book = await clob.book(token_id)

    # Trava 1 — Spread shield (só se max_spread configurado). Ambos os
    # lados devem existir; ausência conta como spread infinito = aborta.
    if max_spread is not None:
        try:
            ask_for_spread = _best_ask_from_book(book)
            bid_for_spread = _best_bid_from_book(book)
        except ValueError:
            log.warning("spread_shield_abort_empty_side", token_id=token_id)
            raise SpreadTooWideError(
                spread=float("inf"), max_spread=max_spread,
            ) from None
        spread = ask_for_spread - bid_for_spread
        if spread > max_spread:
            log.warning(
                "spread_shield_abort",
                token_id=token_id, bid=bid_for_spread, ask=ask_for_spread,
                spread=spread, max_spread=max_spread, side=side,
            )
            raise SpreadTooWideError(spread=spread, max_spread=max_spread)

    # Trava 2 — Regra 1 (âncora no whale_price).
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
