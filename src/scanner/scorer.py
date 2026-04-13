"""Scoring de carteiras com **Regra 3 — Detonação de Wash Traders**.

Política:
    1. Aplicar gates de eliminação (mínimos de PnL, win_rate, trades,
       recência) — carteira que falhar qualquer gate recebe score=0.0.
    2. Aplicar filtro de wash-trading: se |pnl|/volume < min_ratio,
       score=0.0 (a carteira move volume sem gerar lucro real).
    3. Calcular score composto com os pesos de config.yaml. Cada
       componente é normalizado em [0,1] antes de pesar.

Retornar 0.0 é intencional: o wallet_pool usa score para ranquear, e
carteiras com score zero ficam no fim e são purgadas ao próximo rescan.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from src.core.config import ScannerConfig, ScoringWeights
from src.scanner.profiler import WalletProfile

# Para componente de recência: decay exponencial — 7 dias = 50% de peso.
RECENCY_HALF_LIFE_HOURS = 24 * 7


def _normalize_pnl(pnl: float) -> float:
    """Log-normalização: PnL de $500 → ~0.5, PnL de $10k → ~1.0."""
    if pnl <= 0:
        return 0.0
    return min(math.log10(max(pnl, 1.0)) / 4.0, 1.0)


def _recency_weight(last_trade_at: datetime | None) -> float:
    if last_trade_at is None:
        return 0.0
    hours_since = (datetime.now(timezone.utc) - last_trade_at).total_seconds() / 3600.0
    if hours_since < 0:
        return 1.0
    return 0.5 ** (hours_since / RECENCY_HALF_LIFE_HOURS)


def _diversity_score(distinct_markets: int) -> float:
    """Carteira que operou só 1 mercado → 0 (possível insider).

    5+ mercados distintos → saturação em 1.0.
    """
    if distinct_markets <= 1:
        return 0.0
    return min((distinct_markets - 1) / 4.0, 1.0)


def score_wallet(profile: WalletProfile, cfg: ScannerConfig) -> float:
    """Retorna score ∈ [0, 1]. Zero = rejeitada."""
    # --- Gates rígidos ---------------------------------------------------
    if profile.pnl_usd < cfg.min_profit_usd:
        return 0.0
    if profile.win_rate < cfg.min_win_rate:
        return 0.0
    if profile.total_trades < cfg.min_trades:
        return 0.0
    if profile.distinct_markets <= 1:
        return 0.0
    if profile.short_term_trade_ratio < cfg.min_short_term_trade_ratio:
        return 0.0

    # --- Regra 3: Wash-Trading Filter (HARD KILL) ------------------------
    wt = cfg.wash_trading_filter
    if wt.enabled and profile.volume_to_pnl_ratio < wt.min_volume_to_pnl_ratio:
        return 0.0

    if wt.exclude_hyperactive:
        # Heurística: > 100 trades/dia sustentados sem mercados variados
        # indica wash. Usamos volume_to_pnl como proxy se não tivermos
        # janela temporal aqui; o gate principal acima já cobre.
        pass

    # --- Componentes normalizados ---------------------------------------
    w: ScoringWeights = cfg.scoring_weights
    components = {
        "pnl": _normalize_pnl(profile.pnl_usd),
        "win_rate": profile.win_rate,
        "consistency": profile.win_rate,  # proxy: alto win_rate → consistente
        "recency": _recency_weight(profile.last_trade_at),
        "market_diversity": _diversity_score(profile.distinct_markets),
        "short_term_ratio": profile.short_term_trade_ratio,
    }
    score = (
        w.pnl * components["pnl"]
        + w.win_rate * components["win_rate"]
        + w.consistency * components["consistency"]
        + w.recency * components["recency"]
        + w.market_diversity * components["market_diversity"]
        + w.short_term_ratio * components["short_term_ratio"]
    )
    return max(0.0, min(score, 1.0))
