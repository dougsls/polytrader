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

    # On-chain (CTF arbitrage). Polygon mainnet.
    polygon_rpc_url: str = Field(
        default="https://polygon-rpc.com", alias="POLYGON_RPC_URL"
    )
    # Polymarket ConditionalTokens (CTF) — Polygon mainnet
    ctf_contract_address: str = Field(
        default="0x4D97DCd97eC945f40cF65F87097ACe5EA0476045",
        alias="CTF_CONTRACT_ADDRESS",
    )
    # USDC.e (bridged USDC, used by Polymarket)
    usdc_contract_address: str = Field(
        default="0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
        alias="USDC_CONTRACT_ADDRESS",
    )
    # NegRiskCtfExchange / NegRiskAdapter (multi-outcome neg-risk markets)
    neg_risk_adapter_address: str = Field(
        default="0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
        alias="NEG_RISK_ADAPTER_ADDRESS",
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
    # whale_proportional: multiplicador da % que a whale apostou.
    whale_sizing_factor: float = Field(gt=0, le=5.0, default=1.0)
    # Paper-only: BYPASS de TODOS os filtros (slippage/spread/risk/duration/
    # tag exposure/price band). Cada signal vira fake-fill ao preço EXATO
    # da whale. Útil pra observar comportamento bruto. NUNCA ative em LIVE.
    paper_perfect_mirror: bool = False
    # PRESERVAÇÃO DE BANCA — Cofre (30% dos lucros vai pra um bolso
    # intocável, fora do trading). Circuit breaker pausa BUYs se banca
    # ativa cair abaixo de X% da inicial (default 80% = drawdown max 20%).
    profit_safe_pct: float = Field(default=0.0, ge=0.0, le=1.0)
    min_active_bank_pct: float = Field(default=0.0, ge=0.0, le=1.0)
    # Concentração: cap cumulativo POR mercado (absoluto + % banca).
    # Diferente de max_position_usd que é por-fill. Se whale bombardeia um
    # mercado com 20 trades, sem esse cap a posição acumula sem limite.
    # Default 5% = em $500 de banca, max $25 em qualquer mercado único.
    max_position_pct_of_bank: float = Field(default=0.05, ge=0.0, le=0.5)
    # Realismo paper: fees do CLOB aplicadas em cada fill (0.01 = 1%).
    # BUY: paga price*(1+fees). SELL: recebe price*(1-fees). Live real: 0.5-2%.
    paper_apply_fees: float = Field(default=0.0, ge=0.0, le=0.1)
    # Realismo paper: slippage adicional simulando book ilíquido.
    # Ex: 0.015 = entry 1.5% pior que whale. Combina com fees.
    paper_simulate_slippage: float = Field(default=0.0, ge=0.0, le=0.1)
    max_daily_loss_usd: float = Field(gt=0)
    max_drawdown_pct: float = Field(gt=0, le=1.0)
    min_price: float = Field(gt=0, lt=1)
    max_price: float = Field(gt=0, lt=1)
    avoid_resolved_markets: bool
    # Regra 1 — Anti-Slippage Anchoring
    whale_max_slippage_pct: float = Field(gt=0, le=1.0)
    # Escudo de spread dinâmico (ask - bid). 0.02 = 2 centavos.
    # Em paper, até 1.0 (qualquer spread) pra visualizar trades ilíquidos.
    max_spread: float = Field(gt=0, le=1.0, default=0.02)
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
    # HFT — concorrência no consumidor da fila. Default 4 = ~40 ord/s
    # com RTT 100ms NY→London, longe do cap de rate limit Polymarket.
    # Aumentar para 8-16 quando colocar a VPS em Dublin/London.
    max_concurrent_signals: int = Field(ge=1, le=64, default=4)
    # HFT — Optimistic Execution: pula pre-flight /book REST call e
    # dispara FOK direto com limit_price = whale_price ± tolerance.
    # Reduz latência (1 round-trip a menos), mas algumas ordens serão
    # rejeitadas pelo CLOB se o livro andou. Tradeoff de HFT clássico.
    optimistic_execution: bool = False
    # ARQUITETURA — concorrência máxima de chamadas REST de background
    # (Scanner enrich, Resolution batch, snapshot_whale). Hot path (CLOB
    # post_order, RTDS, detect_signal gamma) NÃO passa pelo limiter.
    # 5 concurrent ≈ ~5 reqs/s steady-state. Default conservador para
    # nunca disputar conexões TCP com o trading.
    background_max_concurrent: int = Field(ge=1, le=64, default=5)

    @field_validator("max_price")
    @classmethod
    def _price_band(cls, v: float, info: object) -> float:
        min_p = info.data.get("min_price") if hasattr(info, "data") else None
        if min_p is not None and v <= min_p:
            raise ValueError("max_price deve ser > min_price")
        return v


class ArbitrageConfig(_StrictModel):
    """Track A — pure-arbitrage engine. Independente do copy-trader.

    Edge matemático: se ask_yes + ask_no < 1.0 - fees - safety, comprar
    ambos e fazer mergePositions no CTF Polygon devolve $1 USDC garantido.
    """

    enabled: bool = False
    mode: Literal["live", "paper", "dry-run"] = "paper"
    # Capital próprio reservado para arb (separado do copy-trader).
    max_capital_usd: float = Field(gt=0, default=200.0)
    max_per_op_usd: float = Field(gt=0, default=50.0)
    # Margem de segurança em cima de fees + slippage estimado.
    # Edge bruto = 1 - (ask_yes + ask_no). Edge líquido = bruto - fees - safety.
    # Só executa se edge líquido >= min_edge_pct.
    min_edge_pct: float = Field(gt=0, le=0.5, default=0.005)  # 0.5% edge mínimo
    # CLOB taker fee assumido por leg. Live atual: ~0%.
    # Mantém >0 como safety se fees voltarem.
    fee_per_leg: float = Field(ge=0, le=0.05, default=0.0)
    # Slippage adicional reservado contra book andando contra na execução.
    safety_buffer_pct: float = Field(ge=0, le=0.05, default=0.003)
    # Profundidade mínima de book em cada side antes de tentar.
    min_book_depth_usd: float = Field(ge=0, default=20.0)
    # Janela de scan: mercados que resolvem em mais de N horas são ignorados
    # (capital travado demais; arb só faz sentido em curto prazo).
    max_hours_to_resolution: float = Field(gt=0, default=72.0)
    # Janela de scan: mercados quase resolvendo (< N min) são ignorados
    # (risco de ordem não fillar antes do close).
    min_minutes_to_resolution: float = Field(ge=0, default=10.0)
    # Intervalo entre sweeps (segundos). 30s é razoável; mercados com
    # YES+NO < 1 raramente desaparecem em <30s.
    scan_interval_seconds: int = Field(ge=5, default=30)
    # Categorical (multi-outcome) arb: comprar todos outcomes quando
    # soma < 1. Desligado por default — exige mais cuidado de execução.
    enable_multi_outcome: bool = False
    # Após executar, fazer merge no CTF imediatamente para liberar capital.
    auto_merge: bool = True
    # Concurrent limit: max ops simultâneas em flight (proteção contra
    # mesma whale-event cascateando em N mercados).
    max_concurrent_ops: int = Field(ge=1, default=3)
    # Cool-down: tempo (s) entre 2 ops no mesmo condition_id.
    same_market_cooldown_seconds: int = Field(ge=0, default=60)


class DepthSizingConfig(_StrictModel):
    """Track B — book-depth-aware sizing para o copy-trader.

    Em vez de só validar slippage anchor, lê o livro real e calcula quanto
    consegue encher antes do mid mover X% — evita pendurar GTC em book raso.
    """

    enabled: bool = False
    # % máximo de impacto permitido em cima do mid atual.
    max_impact_pct: float = Field(ge=0, le=0.5, default=0.02)
    # Quantos níveis de book consultar.
    max_levels: int = Field(ge=1, le=20, default=5)


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
    arbitrage: ArbitrageConfig = Field(default_factory=ArbitrageConfig)
    depth_sizing: DepthSizingConfig = Field(default_factory=DepthSizingConfig)


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
