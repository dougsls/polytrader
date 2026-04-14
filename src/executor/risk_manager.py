"""Risk Manager — gates e state de capital.

Checklist por trade:
    1. Score da carteira fonte ≥ min_confidence_score
    2. Preço do outcome em [min_price, max_price]
    3. Mercado não resolvido
    4. Número de posições < max_positions
    5. Daily loss não excedido
    6. Drawdown < max_drawdown_pct
    7. Portfolio + sizing proposto ≤ max_portfolio_usd
    8. Posição proposta ≤ max_position_usd

Qualquer falha → `RiskDecision(allowed=False, reason=...)`.
Quando uma condição de halt global é atingida (daily_loss, drawdown), o
manager memoriza `is_halted=True` e rejeita todo novo trade até reset.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.core.config import ExecutorConfig
from src.core.logger import get_logger
from src.core.models import RiskState, TradeSignal

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    reason: str
    sized_usd: float  # tamanho em USD aprovado


CONSECUTIVE_POST_FAILS_THRESHOLD = 4


class RiskManager:
    def __init__(self, cfg: ExecutorConfig) -> None:
        self._cfg = cfg
        self._halted = False
        self._halt_reason: str | None = None
        self._consecutive_post_fails = 0

    # -- Halt global --------------------------------------------------------

    def halt(self, reason: str) -> None:
        self._halted = True
        self._halt_reason = reason
        log.error("risk_halted", reason=reason)

    def resume(self) -> None:
        self._halted = False
        self._halt_reason = None
        self._consecutive_post_fails = 0

    @property
    def is_halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str | None:
        return self._halt_reason

    # -- Circuit breaker: falhas consecutivas de post_order ---------------

    def record_post_fail(self) -> bool:
        """Incrementa contador. Retorna True se acabou de tripar o breaker."""
        self._consecutive_post_fails += 1
        if self._consecutive_post_fails >= CONSECUTIVE_POST_FAILS_THRESHOLD:
            reason = (
                f"Consecutive post fails: {self._consecutive_post_fails} "
                f">= {CONSECUTIVE_POST_FAILS_THRESHOLD}"
            )
            self.halt(reason)
            return True
        return False

    def record_post_success(self) -> None:
        """Reset do contador quando uma ordem é postada com sucesso."""
        self._consecutive_post_fails = 0

    # -- Sizing -------------------------------------------------------------

    def _size_usd(self, signal: TradeSignal, state: RiskState) -> float:
        mode = self._cfg.sizing_mode
        portfolio = max(state.total_portfolio_value, 1.0)
        if mode == "fixed":
            return self._cfg.fixed_size_usd
        if mode == "proportional":
            return portfolio * self._cfg.proportional_factor
        if mode == "kelly":
            # f* = win_rate - (1-win_rate)/odds. Odds ≈ (1-price)/price.
            price = max(min(signal.price, 0.99), 0.01)
            win = signal.wallet_score
            lose = 1 - win
            odds = (1 - price) / price
            kelly = max(win - lose / odds, 0.0)
            return portfolio * min(kelly, 0.25)
        if mode == "whale_proportional":
            # Espelha a % que a whale apostou DO PORTFOLIO DELA.
            # size_usd = (whale_trade_usd / whale_portfolio_usd) × our_portfolio × factor
            # Se whale_portfolio ausente/zero, fallback para proportional.
            whale_bank = (signal.whale_portfolio_usd or 0.0)
            if whale_bank <= 0 or signal.usd_value <= 0:
                return portfolio * self._cfg.proportional_factor
            conviction = signal.usd_value / whale_bank
            # cap hard em 25% do nosso portfolio pra evitar outlier (whale
            # fazendo all-in num trade de lottery)
            conviction_capped = min(conviction, 0.25)
            return portfolio * conviction_capped * self._cfg.whale_sizing_factor
        return self._cfg.fixed_size_usd

    # -- Checklist ----------------------------------------------------------

    def evaluate(self, signal: TradeSignal, state: RiskState) -> RiskDecision:
        cfg = self._cfg
        if self._halted:
            return RiskDecision(False, f"HALTED: {self._halt_reason}", 0.0)

        if signal.wallet_score < cfg.min_confidence_score:
            return RiskDecision(False, f"LOW_SCORE: {signal.wallet_score:.2f}", 0.0)
        if not (cfg.min_price <= signal.price <= cfg.max_price):
            return RiskDecision(False, f"PRICE_BAND: {signal.price}", 0.0)
        if signal.side == "BUY":
            if state.open_positions >= cfg.max_positions:
                return RiskDecision(False, "MAX_POSITIONS", 0.0)

        if abs(state.daily_pnl) >= cfg.max_daily_loss_usd and state.daily_pnl < 0:
            self.halt("daily_loss_exceeded")
            return RiskDecision(False, "DAILY_LOSS", 0.0)
        if state.current_drawdown >= cfg.max_drawdown_pct:
            self.halt("drawdown_exceeded")
            return RiskDecision(False, "DRAWDOWN", 0.0)

        sized = min(self._size_usd(signal, state), cfg.max_position_usd)
        if state.total_invested + sized > cfg.max_portfolio_usd:
            return RiskDecision(False, "PORTFOLIO_CAP", 0.0)
        if sized <= 0:
            return RiskDecision(False, "ZERO_SIZE", 0.0)

        return RiskDecision(True, "OK", sized)
