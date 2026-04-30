"""Risk Manager — gates e state de capital.

Checklist por trade:
    1. Score da carteira fonte ≥ min_confidence_score
    2. Preço do outcome em [min_price, max_price]
    3. Mercado não resolvido
    4. Número de posições < max_positions (BUY only)
    5. Daily loss não excedido (BUY only — SELL é exit, sempre permitido)
    6. Drawdown < max_drawdown_pct (BUY only — SELL é exit)
    7. Portfolio + sizing proposto ≤ max_portfolio_usd (BUY only)
    8. Posição proposta ≤ max_position_usd

Qualquer falha → `RiskDecision(allowed=False, reason=...)`.

⚠️  RISK MANAGEMENT — SELL bypass durante halt:
    Quando uma condição de halt global é atingida (daily_loss, drawdown,
    consecutive post fails), o manager memoriza `is_halted=True` e
    rejeita NEW BUYs. Mas SELLs (Exit Syncing) são SEMPRE permitidos
    mesmo em halt — caso contrário, em mercado caindo o bot ficaria
    incapaz de liquidar inventário e afundaria com a posição.
    Auditoria: logs `halted_buy_blocked` (rejeitado) vs
    `halted_sell_bypass` (executado).
"""
from __future__ import annotations

from dataclasses import dataclass

from src.core import metrics
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
        # Prometheus gauge — Grafana/Alertmanager consomem isto pra
        # disparar alertas externos quando o bot vai pra halt.
        metrics.halted.set(1)
        log.error("risk_halted", reason=reason)

    def resume(self) -> None:
        self._halted = False
        self._halt_reason = None
        self._consecutive_post_fails = 0
        metrics.halted.set(0)

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
        # ⚠️ ALPHA — Confluence amplifier (BUY only).
        # Quando 2+ whales convergem no mesmo (cid, side) em 15min,
        # cada confluence_count >1 amplifica o sized em +25%, capped 2×.
        # SELL é exit-driven (não convicção) → não amplifica.
        if signal.side == "BUY" and signal.confluence_count > 1:
            confluence_mult = min(
                1.0 + 0.25 * (signal.confluence_count - 1), 2.0,
            )
        else:
            confluence_mult = 1.0
        if mode == "fixed":
            return self._cfg.fixed_size_usd * confluence_mult
        if mode == "proportional":
            return portfolio * self._cfg.proportional_factor * confluence_mult
        if mode == "kelly":
            # f* = p - q/odds. Odds ≈ (1-price)/price.
            #
            # RISK MGMT — usa win_rate PURO da whale (não composta).
            # wallet_score mistura PnL+recência+diversidade+win_rate; usar
            # como `p` na fórmula distorce o sizing em ordens de magnitude.
            # Fallback ao score só se win_rate ausente (logado pra audit).
            price = max(min(signal.price, 0.99), 0.01)
            if signal.whale_win_rate is not None:
                win = signal.whale_win_rate
            else:
                win = signal.wallet_score
                log.warning(
                    "kelly_fallback_to_wallet_score",
                    signal_id=signal.id, wallet_score=signal.wallet_score,
                    note="whale_win_rate ausente; resultado pode estar distorcido",
                )
            # Clamp `win` em [0.01, 0.99] — fora desse range Kelly diverge.
            win = max(0.01, min(win, 0.99))
            lose = 1 - win
            odds = (1 - price) / price
            kelly = max(win - lose / odds, 0.0)
            return portfolio * min(kelly, 0.25) * confluence_mult
        if mode == "whale_proportional":
            # RISK MGMT — Anti-fragmentação: usa o INVENTÁRIO TOTAL da
            # whale no token (whale_total_position_usd, populado pelo
            # signal_detector quando RTDS emite currentSize) em vez do
            # `usd_value` do fill isolado. Sem isso, ordens fragmentadas
            # pelo CLOB (~$100k em 10× $10k) gerariam 10 trades picados
            # com convicção 10× pequena.
            whale_bank = (signal.whale_portfolio_usd or 0.0)
            # Anti-fragmentação: prefere a posição final desejada da whale
            # ao tamanho do fill atomic.
            whale_position_usd = (
                signal.whale_total_position_usd
                if signal.whale_total_position_usd and signal.whale_total_position_usd > 0
                else signal.usd_value
            )
            if whale_bank <= 0 or whale_position_usd <= 0:
                return portfolio * self._cfg.proportional_factor * confluence_mult
            conviction = whale_position_usd / whale_bank
            # cap hard em 25% do nosso portfolio pra evitar outlier (whale
            # fazendo all-in num trade de lottery)
            conviction_capped = min(conviction, 0.25)
            return (
                portfolio * conviction_capped
                * self._cfg.whale_sizing_factor * confluence_mult
            )
        return self._cfg.fixed_size_usd * confluence_mult

    # -- Checklist ----------------------------------------------------------

    def evaluate(self, signal: TradeSignal, state: RiskState) -> RiskDecision:
        """Risk gates com SELL bypass durante halt.

        Ordem dos checks:
            1. Score, price band — sempre aplicados (BUY+SELL).
            2. Halt global: SELL passa (exit always allowed).
            3. BUY-only: max_positions, daily_loss, drawdown, portfolio_cap.
            4. SELL: pula caps de capital novo (não consome banca).
        """
        cfg = self._cfg

        # --- 1. Checks que se aplicam a BUY e SELL ----------------------
        if signal.wallet_score < cfg.min_confidence_score:
            return RiskDecision(False, f"LOW_SCORE: {signal.wallet_score:.2f}", 0.0)
        if not (cfg.min_price <= signal.price <= cfg.max_price):
            return RiskDecision(False, f"PRICE_BAND: {signal.price}", 0.0)

        # --- 2. Halt: BUY rejeitado, SELL liberado (Exit Syncing) -------
        if self._halted:
            if signal.side == "BUY":
                log.warning(
                    "halted_buy_blocked",
                    reason=self._halt_reason, signal_id=signal.id,
                )
                return RiskDecision(False, f"HALTED_BUY: {self._halt_reason}", 0.0)
            # SELL bypass — log + segue pra sizing.
            log.warning(
                "halted_sell_bypass",
                reason=self._halt_reason, signal_id=signal.id,
                wallet=signal.wallet_address[:12],
            )

        # --- 3. BUY-only: caps de capital e exposure --------------------
        if signal.side == "BUY":
            if state.open_positions >= cfg.max_positions:
                return RiskDecision(False, "MAX_POSITIONS", 0.0)

            if abs(state.daily_pnl) >= cfg.max_daily_loss_usd and state.daily_pnl < 0:
                self.halt("daily_loss_exceeded")
                return RiskDecision(False, "DAILY_LOSS", 0.0)
            if state.current_drawdown >= cfg.max_drawdown_pct:
                self.halt("drawdown_exceeded")
                return RiskDecision(False, "DRAWDOWN", 0.0)
        else:
            # --- SELL: triggers ainda RODAM (mantém estado halted pra
            # próximos BUYs), mas NÃO bloqueiam o exit corrente.
            if abs(state.daily_pnl) >= cfg.max_daily_loss_usd and state.daily_pnl < 0:
                self.halt("daily_loss_exceeded")
                # NÃO retorna False — SELL prossegue pra estancar.
            elif state.current_drawdown >= cfg.max_drawdown_pct:
                self.halt("drawdown_exceeded")
                # idem — SELL prossegue.

        # --- 4. Sizing + caps finais ------------------------------------
        sized = min(self._size_usd(signal, state), cfg.max_position_usd)
        # PORTFOLIO_CAP só faz sentido pra BUY (consome novo capital).
        # SELL libera capital — não há cap de saída.
        if signal.side == "BUY":
            if state.total_invested + sized > cfg.max_portfolio_usd:
                return RiskDecision(False, "PORTFOLIO_CAP", 0.0)
        if sized <= 0:
            return RiskDecision(False, "ZERO_SIZE", 0.0)

        return RiskDecision(True, "OK", sized)
