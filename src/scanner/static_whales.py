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
    ("0x6a72f61820b26b1fe4d956e17b6dc2a1ea3033ee", "kch123"),
    ("0x2005d16a84ceefa912d4e380cd32e7ff827875ea", "RN1"),
    ("0x204f72f35326db932158cba6adff0b9a1da95e14", "swisstony"),
    ("0xe90bec87d9ef430f27f9dcfe72c34b76967d5da2", "gmanas"),
    ("0x507e52ef684ca2dd91f90a9d26d149dd3288beae", "GamblingIsAllYouNeed"),
    ("0xdb27bf2ac5d428a9c63dbc914611036855a6c56e", "DrPufferfish"),
    ("0x94a428cfa4f84b264e01f70d93d02bc96cb36356", "GCottrell93"),
    ("0xee613b3fc183ee44f9da9c05f53e2da107e3debf", "sovereign2013"),
    ("0xc2e7800b5af46e6093872b177b7a5e7f0563be51", "beachboy4"),
    ("0x9d84ce0306f8551e02efef1680475fc0f1dc1344", "ImJustKen"),
    ("0x0b9cae2b0dfe7a71c413e0604eaac1c352f87e44", "geniusMC"),
    ("0x5bffcf561bcae83af680ad600cb99f1184d6ffbe", "YatSen"),
    ("0xee00ba338c59557141789b127927a55f5cc5cea1", "S-Works"),
    ("0x03e8a544e97eeff5753bc1e90d46e5ef22af1697", "weflyhigh"),
    ("0xbddf61af533ff524d27154e589d2d7a81510c684", "Countryside"),
    ("0x93abbc022ce98d6f45d4444b594791cc4b7a9723", "gatorr"),
    ("0x2a2c53bd278c04da9962fcf96490e17f3dfb9bc1", "0x2a2C...5461"),
    ("0x44c1dfe43260c94ed4f1d00de2e1f80fb113ebc1", "aenews2"),
    ("0x8c80d213c0cbad777d06ee3f58f6ca4bc03102c3", "SecondWindCapital"),
    ("0xf705fa045201391d9632b7f3cde06a5e24453ca7", "Anon_0xf705"),
    ("0x594edb9112f526fa6a80b8f858a6379c8a2c1c11", "ColdMath"),
    ("0x751a2b86cab503496efd325c8344e10159349ea1", "Sharky6999"),
    # Top de cripto (adicionado 2026-04-14)
    ("0xb27bc932bf8110d8f78e55da7d5f0497a18b5b82", "CryptoWhale"),
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
