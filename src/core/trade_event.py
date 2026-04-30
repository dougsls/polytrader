"""TradeEvent — struct leve para eventos do RTDS / polling.

HFT — Hot path parsing:
    O `detect_signal` é chamado N vezes por segundo (RTDS pico ~3k msgs/s,
    99% droppados pelo Set filter, 1% subscritos). Antes, cada call fazia
    10-15 `dict.get(...)` com fallback chains, alocando strings temporárias
    e dispatching slot lookups múltiplos.

    Solução: parse UMA VEZ no topo do `detect_signal` num `TradeEvent`
    com `__slots__`. Atributos viram offset direto na C-struct subjacente
    do CPython (~30% mais rápido que `dict.get` e zero alocação extra).

    Sem `msgspec`: stack mínima preservada. Para benchmark vs `msgspec.Struct`
    ver os testes em `tests/test_trade_event.py`.

Conversão `dict` → `TradeEvent`:
    Polymarket emite payloads em 3+ shapes diferentes (RTDS, Data API
    polling, market WS). Esta função sabe todos os aliases conhecidos
    e retorna `None` se campos críticos faltarem (early-out).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class TradeEvent:
    """Evento de trade já parseado, normalizado e validado.

    Imutável (frozen) + `__slots__` = zero alocação de __dict__,
    atributos viram lookups O(1) inline em CPython.
    """

    wallet: str               # endereço da baleia, lowercase
    condition_id: str
    token_id: str
    side: Literal["BUY", "SELL"]
    size: float               # quantidade tradeada (não o saldo)
    price: float              # preço executado
    usd_value: float          # size × price (ou usdSize do payload)
    timestamp: int | float | str | None  # cru — detect_signal normaliza p/ datetime
    # HFT — saldo da whale APÓS o trade. Quando disponível (alguns
    # canais Polymarket emitem), permite calcular pct_sold com precisão
    # contra drift do RAM cache. None = fallback ao cálculo legado.
    current_whale_size: float | None


# Tipo permitido pelo CLOB Polymarket. Usado pra early-out.
_VALID_SIDES = frozenset({"BUY", "SELL"})


def parse_trade_event(raw: Mapping[str, Any]) -> TradeEvent | None:
    """Parser tolerante a múltiplos shapes de payload.

    Retorna None se algum campo obrigatório está ausente — caller deve
    tratar como "skip silently". A função NÃO loga (caller decide).

    Aliases reconhecidos (em ordem de preferência):
        wallet:        maker → makerAddress → user
        condition_id:  conditionId → condition_id
        token_id:      asset → tokenId → token_id
        size:          size → amount
        price:         price (default 0)
        usd_value:     size × price (preferido) → usdSize
        timestamp:     timestamp → time → t
        current_whale: currentSize → balanceAfter → postSize → makerBalance
                       (None se ausente; ativa fallback no detect_signal)
    """
    wallet_raw = raw.get("maker") or raw.get("makerAddress") or raw.get("user")
    if not wallet_raw:
        return None
    wallet = wallet_raw.lower() if isinstance(wallet_raw, str) else str(wallet_raw).lower()

    condition_id = raw.get("conditionId") or raw.get("condition_id")
    if not condition_id:
        return None

    token_id = raw.get("asset") or raw.get("tokenId") or raw.get("token_id")
    if not token_id:
        return None

    side_raw = (raw.get("side") or "").upper()
    if side_raw not in _VALID_SIDES:
        return None

    # size + price podem vir como str em alguns canais (RTDS textual).
    size_v = raw.get("size") or raw.get("amount") or 0
    price_v = raw.get("price") or 0
    try:
        size = float(size_v)
        price = float(price_v)
    except (TypeError, ValueError):
        return None

    if price > 0:
        usd_value = size * price
    else:
        usd_value_raw = raw.get("usdSize") or 0
        try:
            usd_value = float(usd_value_raw)
        except (TypeError, ValueError):
            usd_value = 0.0

    # Saldo pós-trade — HFT drift correction. None se ausente.
    # ⚠️ Atenção: `0` é EXATAMENTE o caso crítico (whale zerou). Não usar
    # `or` chain — `0 or X` retorna X em Python. Cascata explícita.
    cur: Any = raw.get("currentSize")
    if cur is None:
        cur = raw.get("balanceAfter")
    if cur is None:
        cur = raw.get("postSize")
    if cur is None:
        cur = raw.get("makerBalance")
    current_whale_size: float | None
    if cur is None:
        current_whale_size = None
    else:
        try:
            current_whale_size = float(cur)
        except (TypeError, ValueError):
            current_whale_size = None

    return TradeEvent(
        wallet=wallet,
        condition_id=str(condition_id),
        token_id=str(token_id),
        side=side_raw,                 # type: ignore[arg-type]  # frozenset garantiu
        size=size,
        price=price,
        usd_value=usd_value,
        timestamp=raw.get("timestamp") or raw.get("time") or raw.get("t"),
        current_whale_size=current_whale_size,
    )
