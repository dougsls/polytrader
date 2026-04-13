"""Diretiva HFT 1 — Quantização de Preço e Tick Size.

A CLOB Engine da Polymarket rejeita (HTTP 400) preços não-arredondados ao
tick_size do ativo. Usar float puro (round()) introduz erros silenciosos de
ponto flutuante (0.1 + 0.2 = 0.30000000000000004) que podem produzir um
preço off-tick por 1 ULP e derrubar a ordem.

Solução: aritmética em Decimal com quantização explícita (ROUND_HALF_EVEN,
o default IEEE 754, que também é o comportamento do CLOB matching engine).
"""
from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, getcontext

getcontext().prec = 28


def _to_decimal(value: float | str | Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def quantize_price(price: float | str | Decimal, tick_size: float | str | Decimal) -> Decimal:
    """Arredonda o preço para o múltiplo mais próximo de tick_size.

    Retorna Decimal (não float) — o caller deve serializar com str(Decimal)
    antes de assinar o EIP-712 para evitar reintrodução de erro ULP.
    """
    p = _to_decimal(price)
    t = _to_decimal(tick_size)
    if t <= 0:
        raise ValueError(f"tick_size inválido: {tick_size}")
    steps = (p / t).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
    return (steps * t).quantize(t, rounding=ROUND_HALF_EVEN)


def quantize_size(size: float | str | Decimal, step: float | str | Decimal) -> Decimal:
    """Quantiza o tamanho da ordem para o step mínimo do ativo.

    Na Polymarket o step de size é tipicamente 0.01 (shares), mas alguns
    mercados usam 1 share. A fonte da verdade é a Gamma API por token_id.
    """
    s = _to_decimal(size)
    st = _to_decimal(step)
    if st <= 0:
        raise ValueError(f"size step inválido: {step}")
    steps = (s / st).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
    return (steps * st).quantize(st, rounding=ROUND_HALF_EVEN)
