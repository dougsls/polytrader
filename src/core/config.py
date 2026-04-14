"""Carregador unificado: .env (pydantic-settings) + config.yaml (PyYAML).

Estrutura:
    Settings
      ├── EnvSettings    — lida do .env (secrets, VPS, telegram)
      └── YamlConfig     — scanner/tracker/executor/dashboard/notifier

O YAML é validado com Pydantic; qualquer campo faltando/tipado errado
falha no startup (fail-fast, melhor que descobrir no primeiro trade).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


# ---------- .env (secrets & VPS) ----------


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(DEFAULT_ENV_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Wallet
    private_key: str = Field(default="", alias="PRIVATE_KEY")
    funder_address: str = Field(default="", alias="FUNDER_ADDRESS")
    signature_type: int = Field(default=0, alias="SIGNATURE_TYPE")

    # Exchange
    exchange_mode: Literal["international", "us"] = Field(
        default="international", alias="EXCHANGE_MODE"
    )

    # Telegram
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")

    # VPS / infra
    vps_provider: str = Field(default="quantvps", alias="VPS_PROVIDER")
    vps_location: str = Field(default="new-york", alias="VPS_LOCATION")
    vps_timezone: str = Field(default="UTC", alias="VPS_TIMEZONE")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    dashboard_port: int = Field(default=8080, alias="DASHBOARD_PORT")
    dashboard_host: str = Field(default="0.0.0.0", alias="DASHBOARD_HOST")
    dashboard_secret: str = Field(default="", alias="DASHBOARD_SECRET")
    dashboard_user: str = Field(default="operator", alias="DASHBOARD_USER")

    # Latency
    latency_alert_threshold_ms: int = Field(default=200, alias="LATENCY_ALERT_THRESHOLD_MS")
    latency_probe_interval_seconds: int = Field(
        default=300, alias="LATENCY_PROBE_INTERVAL_SECONDS"
    )


# ---------- config.yaml ----------


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScoringWeights(_StrictModel):
    pnl: float
    win_rate: float
    consistency: float
    recency: float
    market_diversity: float
    short_term_ratio: float

    @field_validator("short_term_ratio")
    @classmethod
    def _weights_sum_to_one(cls, v: float, info: object) -> float:
        # Validado no modelo pai; aqui apenas no final.
        return v


class WashTradingFilter(_StrictModel):
    enabled: bool
    min_volume_to_pnl_ratio: float = Field(gt=0)
    exclude_hyperactive: bool


class ScannerConfig(_StrictModel):
    enabled: bool
    scan_interval_minutes: int = Field(ge=1)
    leaderboard_periods: list[str]
    min_profit_usd: float
    min_win_rate: float = Field(ge=0.0, le=1.0)
    min_trades: int = Field(ge=0)
    max_wallets_tracked: int = Field(ge=1)
    scoring_weights: ScoringWeights
    wash_trading_filter: WashTradingFilter
    min_short_term_trade_ratio: float = Field(ge=0.0, le=1.0)
    short_term_threshold_hours: int = Field(ge=1)
    # Fallback de whitelist manual — usado quando a Polymarket API de
    # leaderboard não estiver disponível (ex: endpoint removido). Lista
    # de endereços Ethereum (0x...) que o bot rastreia direto.
    manual_whitelist: list[str] = Field(default_factory=list)

    @field_validator("scoring_weights")
    @classmethod
    def _sum_weights(cls, w: ScoringWeights) -> ScoringWeights:
        total = (
            w.pnl + w.win_rate + w.consistency + w.recency
            + w.market_diversity + w.short_term_ratio
        )
        if abs(total - 1.0) > 0.001:
            raise ValueError(f"scoring_weights somam {total}, deve ser 1.0")
        return w


class MarketDurationFilter(_StrictModel):
    enabled: bool
    max_hours_to_resolution: float = Field(gt=0)
    min_hours_to_resolution: float = Field(ge=0)
    prefer_range_hours: list[float]
    hard_block_days: int = Field(ge=1)
    check_end_date_field: str
    check_expiration_field: str
    fallback_behavior: Literal["skip", "allow"]


class TrackerConfig(_StrictModel):
    enabled: bool
    poll_interval_seconds: int = Field(ge=1)
    use_websocket: bool
    signal_max_age_seconds: int = Field(ge=1)
    min_trade_size_usd: float = Field(ge=0)
    market_duration_filter: MarketDurationFilter


class ExecutorConfig(_StrictModel):
    enabled: bool
    mode: Literal["live", "paper", "dry-run"]
    max_portfolio_usd: float = Field(gt=0)
    max_position_usd: float = Field(gt=0)
    max_positions: int = Field(ge=1)
    sizing_mode: Literal["fixed", "proportional", "kelly", "whale_proportional"]
    fixed_size_usd: float = Field(gt=0)
    proportional_factor: float = Field(gt=0, le=1.0)
    # whale_proportional: multiplicador da % que a whale apostou do portfolio
    # dela. 1.0 = espelho exato (se whale arrisca 5% do bank → nós também).
    # >1.0 = amplificar convicção. <1.0 = atenuar.
    whale_sizing_factor: float = Field(gt=0, le=5.0, default=1.0)
    max_daily_loss_usd: float = Field(gt=0)
    max_drawdown_pct: float = Field(gt=0, le=1.0)
    min_price: float = Field(gt=0, lt=1)
    max_price: float = Field(gt=0, lt=1)
    avoid_resolved_markets: bool
    # Regra 1 — Anti-Slippage Anchoring
    whale_max_slippage_pct: float = Field(gt=0, le=0.5)
    # Escudo de spread dinâmico (ask - bid). 0.02 = 2 centavos.
    max_spread: float = Field(gt=0, le=0.5, default=0.02)
    default_order_type: str
    limit_price_offset: float = Field(ge=0)
    fok_fallback: bool
    fok_fallback_timeout_seconds: int = Field(ge=1)
    max_position_age_hours: float = Field(gt=0, default=48.0)
    # H6 — cap de exposure por tag/categoria (ex: "politics", "sports").
    # 0.30 = nenhuma tag pode concentrar mais que 30% do portfolio.
    max_tag_exposure_pct: float = Field(gt=0, le=1.0, default=0.30)
    copy_buys: bool
    copy_sells: bool
    min_confidence_score: float = Field(ge=0.0, le=1.0)
    enforce_market_duration: bool
    skip_reason_on_long_market: str

    @field_validator("max_price")
    @classmethod
    def _price_band(cls, v: float, info: object) -> float:
        min_p = info.data.get("min_price") if hasattr(info, "data") else None
        if min_p is not None and v <= min_p:
            raise ValueError("max_price deve ser > min_price")
        return v


class DashboardConfig(_StrictModel):
    enabled: bool
    host: str
    port: int = Field(ge=1, le=65535)
    refresh_interval_seconds: int = Field(ge=1)
    require_auth: bool


class NotifierConfig(_StrictModel):
    enabled: bool
    notify_on_trade: bool
    notify_on_new_wallet: bool
    notify_on_risk_alert: bool
    notify_on_daily_summary: bool
    daily_summary_hour: int = Field(ge=0, le=23)


class YamlConfig(_StrictModel):
    scanner: ScannerConfig
    tracker: TrackerConfig
    executor: ExecutorConfig
    dashboard: DashboardConfig
    notifier: NotifierConfig


# ---------- Façade ----------


class Settings(_StrictModel):
    env: EnvSettings
    config: YamlConfig


def load_yaml_config(path: Path = DEFAULT_CONFIG_PATH) -> YamlConfig:
    if not path.is_file():
        raise FileNotFoundError(f"config.yaml não encontrado em {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"config.yaml inválido (esperado dict, veio {type(raw).__name__})")
    return YamlConfig.model_validate(raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(env=EnvSettings(), config=load_yaml_config())
