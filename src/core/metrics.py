"""Prometheus metrics — contadores HFT leves.

Expostos via `/metrics` no dashboard FastAPI. Grafana/Alertmanager
consomem sem depender de journalctl ou Telegram.

Uso:
    from src.core.metrics import signals_received, trades_executed, errors
    signals_received.inc()
    trades_executed.inc()
    errors.labels(source="executor").inc()

Guidelines:
    - Incremento é atômico no cliente Python (GIL) — seguro em asyncio.
    - Labels = baixa cardinalidade (source: rtds/scanner/tracker/executor).
    - NÃO adicionar labels com IDs únicos (token_id, signal_id) — explode memória.
"""
from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

# Registry próprio — evita colisão com outros serviços no mesmo processo.
REGISTRY = CollectorRegistry()

signals_received = Counter(
    "polytrader_signals_received_total",
    "Sinais que passaram pelo filtro Set do RTDS e foram enfileirados.",
    registry=REGISTRY,
)

signals_dropped = Counter(
    "polytrader_signals_dropped_total",
    "Sinais descartados por backpressure (queue full).",
    registry=REGISTRY,
)

trades_executed = Counter(
    "polytrader_trades_executed_total",
    "Trades que saíram do CopyEngine com status=executed.",
    registry=REGISTRY,
)

trades_skipped = Counter(
    "polytrader_trades_skipped_total",
    "Sinais rejeitados pelo CopyEngine.",
    ["reason_class"],
    registry=REGISTRY,
)

errors = Counter(
    "polytrader_errors_total",
    "Exceções capturadas em cada camada.",
    ["source"],  # rtds, scanner, tracker, executor, dashboard
    registry=REGISTRY,
)

queue_size = Gauge(
    "polytrader_signal_queue_size",
    "Tamanho atual da asyncio.Queue de sinais.",
    registry=REGISTRY,
)

halted = Gauge(
    "polytrader_halted",
    "1 se o RiskManager está em halt, 0 caso contrário.",
    registry=REGISTRY,
)

signal_to_fill_seconds = Histogram(
    "polytrader_signal_to_fill_seconds",
    "Latência do handle_signal (RTDS → apply_fill). Mede CLOB signing + net.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0),
    registry=REGISTRY,
)


def render_metrics() -> bytes:
    """Renderiza todos os counters no formato Prometheus exposition."""
    return generate_latest(REGISTRY)
