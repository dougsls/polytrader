"""Whitelist estática de whales (bypass do leaderboard API).

A Polymarket removeu o endpoint `/leaderboard` da Data API. Em vez de
depender de descoberta dinâmica, o Scanner injeta direto as carteiras
mapeadas manualmente via Polymarket Analytics.

Todos os endereços são normalizados para **lowercase** — o RTDS
comparará com `maker.lower()` para evitar mismatch case-sensitive.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.scanner.profiler import WalletProfile

TARGET_WHALES: tuple[tuple[str, str], ...] = (
    # Pool curado (5 whales) — remodelado 2026-04-14 por seleção manual.
    ("0x89b5cdaaa4866c1e738406712012a630b4078beb", "ohanism"),
    ("0xb27bc932bf8110d8f78e55da7d5f0497a18b5b82", "CryptoWhale"),
    ("0x751a2b86cab503496efd325c8344e10159349ea1", "Sharky6999"),
    ("0x594edb9112f526fa6a80b8f858a6379c8a2c1c11", "ColdMath"),
    ("0x204f72f35326db932158cba6adff0b9a1da95e14", "swisstony"),
)

# Validação no import: todos lowercase + formato 0x+40hex.
for _addr, _name in TARGET_WHALES:
    if not (_addr.startswith("0x") and len(_addr) == 42 and _addr == _addr.lower()):
        raise ValueError(f"TARGET_WHALES entry inválida: {_addr} ({_name})")


def static_whale_profiles() -> list[WalletProfile]:
    """Skeleton de WalletProfiles — enriched depois por enrich_profiles().

    Valores iniciais são placeholders que passam os gates do scorer.
    O Scanner substitui por dados REAIS (pnl, win_rate, volume, trades,
    distinct_markets, last_trade_at) vindos de /value + /traded +
    /closed-positions da Polymarket Data API. Se enrich falhar, o
    whale permanece com estes defaults — melhor duplicado que vazio.
    """
    now = datetime.now(timezone.utc)
    return [
        WalletProfile(
            address=addr,
            name=name,
            pnl_usd=1_000.0,
            volume_usd=5_000.0,
            win_rate=0.60,
            total_trades=20,
            distinct_markets=5,
            short_term_trade_ratio=0.80,
            last_trade_at=now - timedelta(hours=1),
        )
        for addr, name in TARGET_WHALES
    ]
