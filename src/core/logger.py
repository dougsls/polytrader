"""Structured logging — JSON em stdout para journalctl.

A VPS roda systemd; stdout é capturado pelo journal. Filtros comuns:
    journalctl -u polytrader -f | grep '"level":"error"'
    journalctl -u polytrader -f | jq 'select(.event=="trade_executed")'

Uso:
    from src.core.logger import get_logger, configure_logging
    configure_logging("INFO")
    log = get_logger(__name__)
    log.info("signal_detected", wallet=addr, condition_id=cid, usd=42.5)
"""
from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def configure_logging(level: str = "INFO") -> None:
    """Idempotente. Chamar uma vez no startup (main.py)."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
