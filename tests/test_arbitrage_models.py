"""Testes dos modelos Pydantic da arb + slippage helpers HFT."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.arbitrage.models import ArbExecution, ArbLegFill, ArbOpportunity


def test_arb_opportunity_actionable():
    op = ArbOpportunity(
        id="abc",
        condition_id="0xfeed",
        yes_token_id="1",
        no_token_id="2",
        ask_yes=0.45,
        ask_no=0.50,
        depth_yes_usd=100,
        depth_no_usd=200,
        edge_gross_pct=0.05,
        edge_net_pct=0.03,
        suggested_size_usd=50,
        detected_at=datetime.now(timezone.utc),
    )
    assert op.is_actionable
    op2 = op.model_copy(update={"status": "skipped"})
    assert not op2.is_actionable


def test_arb_opportunity_rejects_naive_datetime():
    with pytest.raises(Exception):
        ArbOpportunity(
            id="abc",
            condition_id="0xfeed",
            yes_token_id="1",
            no_token_id="2",
            ask_yes=0.45,
            ask_no=0.50,
            depth_yes_usd=100,
            depth_no_usd=200,
            edge_gross_pct=0.05,
            edge_net_pct=0.03,
            suggested_size_usd=50,
            detected_at=datetime.now(),  # naive — deve falhar
        )


def test_arb_execution_terminal_states():
    leg = ArbLegFill(side="YES", token_id="1", intended_size=10, intended_price=0.4)
    e = ArbExecution(
        id="x",
        opportunity_id="o",
        condition_id="c",
        yes_leg=leg,
        no_leg=ArbLegFill(side="NO", token_id="2", intended_size=10, intended_price=0.5),
        started_at=datetime.now(timezone.utc),
    )
    assert not e.is_terminal
    e.status = "completed"
    assert e.is_terminal
    e.status = "rolled_back"
    assert e.is_terminal
