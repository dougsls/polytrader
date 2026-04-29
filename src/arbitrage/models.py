"""Pydantic models para o arbitrage engine.

Convenção: timezone-aware UTC sempre (herda _BaseModel do core).
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, Field

from src.core.models import _BaseModel

ArbOpStatus = Literal["pending", "executing", "executed", "skipped", "expired", "failed"]
ArbExecStatus = Literal["pending", "completed", "partial", "failed", "rolled_back"]
LegStatus = Literal["pending", "submitted", "filled", "partial", "failed", "cancelled"]
MergeStatus = Literal["pending", "confirmed", "failed", "skipped"]


class ArbOpportunity(_BaseModel):
    """Oportunidade detectada pelo scanner. Ainda não executada."""

    id: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    market_title: str | None = None
    ask_yes: float = Field(gt=0, lt=1)
    ask_no: float = Field(gt=0, lt=1)
    depth_yes_usd: float = Field(ge=0)
    depth_no_usd: float = Field(ge=0)
    edge_gross_pct: float
    edge_net_pct: float
    suggested_size_usd: float = Field(ge=0)
    hours_to_resolution: float | None = None
    detected_at: datetime
    status: ArbOpStatus = "pending"
    skip_reason: str | None = None

    @property
    def is_actionable(self) -> bool:
        return self.status == "pending" and self.edge_net_pct > 0


class ArbLegFill(_BaseModel):
    """Fill de uma leg da arb (YES ou NO)."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    side: Literal["YES", "NO"]
    token_id: str
    order_id: str | None = None
    intended_size: float
    intended_price: float
    fill_price: float | None = None
    fill_size: float | None = None
    status: LegStatus = "pending"
    error: str | None = None


class ArbExecution(_BaseModel):
    """Execução completa: 2 ordens CLOB + merge on-chain."""

    id: str
    opportunity_id: str
    condition_id: str
    invested_usd: float | None = None
    yes_leg: ArbLegFill
    no_leg: ArbLegFill
    merge_tx_hash: str | None = None
    merge_status: MergeStatus = "pending"
    merge_amount_usd: float | None = None
    realized_pnl_usd: float | None = None
    status: ArbExecStatus = "pending"
    error: str | None = None
    started_at: datetime
    completed_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in ("completed", "failed", "rolled_back")
