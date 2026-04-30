"""Modelos Pydantic v2 do domínio — source of truth de serialização.

Usados pelos clients (entrada/saída HTTP), pelo DB layer e pelo dashboard.
Tudo em timezone-aware UTC; qualquer datetime naive é erro.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Side = Literal["BUY", "SELL"]
SignalStatus = Literal["pending", "executing", "executed", "skipped", "failed"]
TradeStatus = Literal["pending", "submitted", "filled", "partial", "failed", "cancelled"]
SignalSource = Literal["polling", "websocket"]


class _BaseModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=False,
        arbitrary_types_allowed=True,
        str_strip_whitespace=True,
    )

    @field_validator("*", mode="before")
    @classmethod
    def _reject_naive_datetime(cls, v: object) -> object:
        if isinstance(v, datetime) and v.tzinfo is None:
            raise ValueError("datetime sem tzinfo — use UTC-aware")
        return v


class TrackedWallet(_BaseModel):
    address: str
    name: str | None = None
    score: float = Field(ge=0.0, le=1.0, default=0.0)
    pnl_usd: float = 0.0
    win_rate: float = Field(ge=0.0, le=1.0, default=0.0)
    total_trades: int = Field(ge=0, default=0)
    tracked_since: datetime
    is_active: bool = True
    last_trade_at: datetime | None = None


class TradeSignal(_BaseModel):
    id: str
    wallet_address: str
    wallet_score: float
    condition_id: str
    token_id: str
    side: Side
    size: float
    price: float  # preço EXECUTADO pela baleia — âncora da Regra 1
    usd_value: float
    market_title: str
    outcome: str
    market_end_date: datetime | None = None
    hours_to_resolution: float | None = None
    detected_at: datetime
    source: SignalSource
    status: SignalStatus = "pending"
    skip_reason: str | None = None
    # Portfolio da whale (volume_usd do enrich). Usado por whale_proportional
    # para calcular convicção: (trade_usd / whale_portfolio) × our_portfolio.
    whale_portfolio_usd: float | None = None
    # RISK MGMT — Win rate histórico PURO da whale (sem mistura com PnL/
    # recency/diversidade do score composto). Usado pelo Kelly Criterion:
    #   f* = win_rate - (1-win_rate)/odds
    # Score composto distorce a fórmula (mistura ranks heterogêneos).
    # None = fallback ao wallet_score com warning (compat retroativa).
    whale_win_rate: float | None = None
    # RISK MGMT — Inventário da whale neste token APÓS o trade (em USD).
    # Anti-fragmentação: ordens grandes são partidas em múltiplos fills no
    # CLOB; usar `usd_value` do fill isolado faz o bot calcular convicção
    # baixa N vezes. Esse campo carrega `current_whale_size × price` quando
    # o payload do RTDS emite `currentSize` — convicção real = posição
    # final desejada / whale_bank.
    whale_total_position_usd: float | None = None
    # RISK MGMT — Stop-loss em tempo (stale cleanup). Quando True,
    # copy_engine pula a Regra 1 (Anti-Slippage Anchoring). Usado APENAS
    # para SELLs sintéticos de stale_cleanup_loop após max_position_age_hours.
    # Sem isso, posições compradas alto e que despencaram ficam presas
    # eternamente — Regra 1 calcula slippage=90% e aborta. É um dump-out
    # forçado pra liberar capital. Spread shield ainda valida (book vazio
    # ainda aborta). NUNCA setado por sinais reais de copy/arb.
    bypass_slippage_check: bool = False
    # ⚠️ ALPHA — Convicção via consenso. Quantas wallets distintas do
    # nosso pool entraram no MESMO (condition_id, side) numa janela de
    # 15min. 1 = só esta whale (default). 2+ = confluência → RiskManager
    # amplifica o sizing (até 2x). Captura alpha quando múltiplas mentes
    # convergem no mesmo edge.
    confluence_count: int = 1


class CopyTrade(_BaseModel):
    id: str
    signal_id: str
    order_id: str | None = None
    condition_id: str
    token_id: str
    side: Side
    intended_size: float
    executed_size: float | None = None
    intended_price: float
    executed_price: float | None = None
    slippage: float | None = None
    status: TradeStatus = "pending"
    created_at: datetime
    filled_at: datetime | None = None
    error: str | None = None


class BotPosition(_BaseModel):
    condition_id: str
    token_id: str
    market_title: str
    outcome: str
    size: float
    avg_entry_price: float
    current_price: float | None = None
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    source_wallets: list[str] = Field(default_factory=list)
    opened_at: datetime
    closed_at: datetime | None = None
    is_open: bool = True


class RiskState(_BaseModel):
    total_portfolio_value: float
    total_invested: float
    total_unrealized_pnl: float
    total_realized_pnl: float
    daily_pnl: float
    max_drawdown: float
    current_drawdown: float
    open_positions: int
    is_halted: bool = False
    halt_reason: str | None = None


class MarketSpec(_BaseModel):
    """Metadados quantitativos (Gamma API) consumidos pelo order_builder.

    tick_size e neg_risk vêm da tabela market_metadata_cache
    (Diretivas HFT 1 e 4).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    condition_id: str
    token_id: str
    tick_size: Decimal
    size_step: Decimal = Decimal("0.01")
    neg_risk: bool = False
    end_date: datetime | None = None
