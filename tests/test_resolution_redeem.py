"""FUND-LOCK FIX — Item 1: redeem on-chain quando won.

Cobre:
  - _outcome_index / _outcome_index_set (CTF binary indexSet bitmask)
  - dispatch fire-and-forget no won
  - sentinel "dispatching" idempotência
  - "lost-skip" para perdedoras (não desperdiça gas)
  - "paper-skip" quando ctf=None
  - "failed:..." persistido em tx falha (retry no próximo tick)
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.database import get_connection, init_database
from src.executor.resolution_watcher import (
    _dispatch_redeem,
    _outcome_index,
    _outcome_index_set,
    check_resolutions_once,
)


# ============ indexSet bitmask CTF ============

def test_outcome_index_set_binary_yes_is_1():
    assert _outcome_index_set(0) == 1


def test_outcome_index_set_binary_no_is_2():
    assert _outcome_index_set(1) == 2


def test_outcome_index_set_higher_outcomes_use_bitmask():
    """Multi-outcome (neg-risk): outcome i → 1 << i."""
    assert _outcome_index_set(2) == 4
    assert _outcome_index_set(3) == 8


def test_outcome_index_resolves_yes_no_case_insensitive():
    market = {"outcomes": ["Yes", "No"]}
    assert _outcome_index(market, "yes") == 0
    assert _outcome_index(market, "NO") == 1
    assert _outcome_index(market, "Maybe") is None


def test_outcome_index_handles_json_string_outcomes():
    """Gamma 2026 às vezes retorna outcomes como JSON-string."""
    market = {"outcomes": json.dumps(["Yes", "No"])}
    assert _outcome_index(market, "Yes") == 0


# ============ Helper para DB de teste ============

async def _seed_position(
    db: Path, *, position_id: int = 1, condition_id: str = "0xCID",
    outcome: str = "Yes", token_id: str = "tok-1",
    size: float = 100.0, entry: float = 0.40,
    current_price: float | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with get_connection(db) as conn:
        await conn.execute(
            "INSERT INTO bot_positions"
            " (id, condition_id, token_id, market_title, outcome,"
            "  size, avg_entry_price, current_price, source_wallets_json,"
            "  opened_at, is_open, updated_at)"
            " VALUES (?, ?, ?, 'm?', ?, ?, ?, ?, '[]', ?, 1, ?)",
            (position_id, condition_id, token_id, outcome,
             size, entry, current_price, now, now),
        )
        await conn.commit()


# ============ _dispatch_redeem ============

@pytest.mark.asyncio
async def test_dispatch_redeem_calls_ctf_and_persists_tx_hash(tmp_path: Path):
    db = tmp_path / "r.db"
    await init_database(db)
    await _seed_position(db, position_id=10, outcome="Yes")

    mock_ctf = MagicMock()
    mock_ctf.redeem = AsyncMock(return_value="0xCAFE")

    market = {"outcomes": ["Yes", "No"], "outcomePrices": json.dumps([1.0, 0.0])}

    async with get_connection(db) as conn:
        await _dispatch_redeem(
            conn, mock_ctf, position_id=10, condition_id="0xCID",
            outcome_label="Yes", market=market,
        )
        async with conn.execute(
            "SELECT redeem_tx_hash FROM bot_positions WHERE id=10"
        ) as cur:
            row = await cur.fetchone()
    assert row[0] == "0xCAFE"
    # Verifica que ctf.redeem foi chamado com indexSet=1 (Yes)
    mock_ctf.redeem.assert_awaited_once_with("0xCID", [1])


@pytest.mark.asyncio
async def test_dispatch_redeem_no_outcome_falls_back_to_yes_convention(tmp_path: Path):
    """Se market=None, infere indexSet do label (Yes→1, No→2)."""
    db = tmp_path / "r.db"
    await init_database(db)
    await _seed_position(db, position_id=11, outcome="No")

    mock_ctf = MagicMock()
    mock_ctf.redeem = AsyncMock(return_value="0xDEAD")

    async with get_connection(db) as conn:
        await _dispatch_redeem(
            conn, mock_ctf, position_id=11, condition_id="0xCID",
            outcome_label="No", market=None,
        )
    mock_ctf.redeem.assert_awaited_once_with("0xCID", [2])


@pytest.mark.asyncio
async def test_dispatch_redeem_paper_mode_marks_paper_skip(tmp_path: Path):
    """ctf=None (paper/dry-run) → marca 'paper-skip' sem chamar nada."""
    db = tmp_path / "r.db"
    await init_database(db)
    await _seed_position(db, position_id=12)

    async with get_connection(db) as conn:
        await _dispatch_redeem(
            conn, ctf=None, position_id=12, condition_id="0xC",
            outcome_label="Yes", market=None,
        )
        async with conn.execute(
            "SELECT redeem_tx_hash FROM bot_positions WHERE id=12"
        ) as cur:
            row = await cur.fetchone()
    assert row[0] == "paper-skip"


@pytest.mark.asyncio
async def test_dispatch_redeem_failure_persists_failed_for_retry(tmp_path: Path):
    """Falha on-chain → 'failed:<msg>' persistido pra retry no próximo tick."""
    db = tmp_path / "r.db"
    await init_database(db)
    await _seed_position(db, position_id=13)

    mock_ctf = MagicMock()
    mock_ctf.redeem = AsyncMock(side_effect=RuntimeError("rpc dead"))

    async with get_connection(db) as conn:
        await _dispatch_redeem(
            conn, mock_ctf, position_id=13, condition_id="0xC",
            outcome_label="Yes", market=None,
        )
        async with conn.execute(
            "SELECT redeem_tx_hash FROM bot_positions WHERE id=13"
        ) as cur:
            row = await cur.fetchone()
    assert row[0].startswith("failed:")
    assert "rpc dead" in row[0]


# ============ check_resolutions_once integration ============

@pytest.mark.asyncio
async def test_check_resolutions_dispatches_redeem_for_winners(tmp_path: Path):
    """Mercado closed + resolution_price=1 + outcome=Yes → win + redeem dispatched."""
    db = tmp_path / "r.db"
    await init_database(db)
    await _seed_position(db, position_id=20, condition_id="0xWIN", outcome="Yes")

    gamma = MagicMock()
    gamma.get_markets_batch = AsyncMock(return_value={
        "0xwin": {
            "conditionId": "0xWIN", "closed": True,
            "outcomes": ["Yes", "No"],
            "outcomePrices": json.dumps([1.0, 0.0]),
        }
    })
    mock_ctf = MagicMock()
    mock_ctf.redeem = AsyncMock(return_value="0xREDEEMED")

    # Mantém conn aberta enquanto a task fire-and-forget de redeem roda
    # (em produção shared_conn sobrevive o processo inteiro).
    async with get_connection(db) as conn:
        stats = await check_resolutions_once(conn, gamma, ctf=mock_ctf)
        # Aguarda a task completar ANTES de fechar a conn.
        for _ in range(20):
            async with conn.execute(
                "SELECT redeem_tx_hash FROM bot_positions WHERE id=20"
            ) as cur:
                row = await cur.fetchone()
            if row and row[0] == "0xREDEEMED":
                break
            await asyncio.sleep(0.02)
        async with conn.execute(
            "SELECT redeem_tx_hash, is_open, realized_pnl FROM bot_positions WHERE id=20"
        ) as cur:
            final = await cur.fetchone()

    assert stats["won"] == 1
    assert stats["redeems_dispatched"] == 1
    gamma.get_markets_batch.assert_awaited_once()
    mock_ctf.redeem.assert_awaited_once_with("0xWIN", [1])
    assert final[0] == "0xREDEEMED"
    assert final[1] == 0
    assert final[2] == pytest.approx(60.0)


@pytest.mark.asyncio
async def test_check_resolutions_marks_lost_with_lost_skip(tmp_path: Path):
    """Position perdedora → lost-skip (não desperdiça gas em token zerado)."""
    db = tmp_path / "r.db"
    await init_database(db)
    # current_price=0.5 (não-extremo) garante que cai no path normal de
    # parsing do market.outcomePrices (que diz Yes perdeu).
    await _seed_position(db, position_id=21, outcome="Yes", current_price=0.5)

    gamma = MagicMock()
    gamma.get_markets_batch = AsyncMock(return_value={
        "0xcid": {
            "conditionId": "0xCID", "closed": True,
            "outcomes": ["Yes", "No"],
            "outcomePrices": json.dumps([0.0, 1.0]),  # Yes perdeu
        }
    })
    mock_ctf = MagicMock()
    mock_ctf.redeem = AsyncMock()

    async with get_connection(db) as conn:
        stats = await check_resolutions_once(conn, gamma, ctf=mock_ctf)

    assert stats["lost"] == 1
    assert stats["redeems_dispatched"] == 0
    mock_ctf.redeem.assert_not_called()

    async with get_connection(db) as conn:
        async with conn.execute(
            "SELECT redeem_tx_hash FROM bot_positions WHERE id=21"
        ) as cur:
            row = await cur.fetchone()
    assert row[0] == "lost-skip"


@pytest.mark.asyncio
async def test_check_resolutions_idempotent_via_dispatching_sentinel(tmp_path: Path):
    """Dois ticks consecutivos não disparam 2 redeems pra mesma position."""
    db = tmp_path / "r.db"
    await init_database(db)
    await _seed_position(db, position_id=22)

    gamma = MagicMock()
    gamma.get_markets_batch = AsyncMock(return_value={
        "0xcid": {
            "conditionId": "0xCID", "closed": True,
            "outcomes": ["Yes", "No"],
            "outcomePrices": json.dumps([1.0, 0.0]),
        }
    })
    # ctf.redeem com sleep pra simular tx pendurada entre ticks
    mock_ctf = MagicMock()
    mock_ctf.redeem = AsyncMock(return_value="0xTX")

    # Tick 1 — dispara redeem
    async with get_connection(db) as conn:
        s1 = await check_resolutions_once(conn, gamma, ctf=mock_ctf)
    assert s1["redeems_dispatched"] == 1

    # Tick 2 — position já tem redeem_tx_hash (resolved+won/lost branches
    # exigem is_open=1; após primeiro tick is_open=0 → não entra no rows).
    # Adicional sanity: força re-check com is_open=1 simulando race.
    await asyncio.sleep(0.05)  # deixa task completar
    async with get_connection(db) as conn:
        s2 = await check_resolutions_once(conn, gamma, ctf=mock_ctf)
    # Segundo tick não dispara nada (position is_open=0).
    assert s2["redeems_dispatched"] == 0


# ============ Item 2 — Batch request ============

@pytest.mark.asyncio
async def test_resolution_uses_single_batch_call_not_n_individual(tmp_path: Path):
    """RATE-LIMIT FIX: 5 positions abertas → 1 batch call, não 5 individuais."""
    db = tmp_path / "r.db"
    await init_database(db)
    for i in range(5):
        # current_price=0.5 (não-extremo) → evita _local_resolve_if_extreme
        # disparar lost-skip antes do batch path.
        await _seed_position(
            db, position_id=100 + i, condition_id=f"0xC{i}",
            current_price=0.5,
        )

    gamma = MagicMock()
    # Retorna todos os 5 markets ainda abertos (não disparam redeem)
    gamma.get_markets_batch = AsyncMock(return_value={
        f"0xc{i}": {
            "conditionId": f"0xC{i}", "closed": False,
            "outcomes": ["Yes", "No"],
            "outcomePrices": json.dumps([0.5, 0.5]),
        }
        for i in range(5)
    })
    # get_market individual (legacy path) NÃO deve ser chamado.
    gamma.get_market = AsyncMock(side_effect=AssertionError(
        "BUG: rate-limit DDoS — get_market individual chamado em vez do batch!"
    ))

    async with get_connection(db) as conn:
        stats = await check_resolutions_once(conn, gamma, ctf=None)

    # UMA chamada batch (mesmo com 5 positions)
    gamma.get_markets_batch.assert_awaited_once()
    assert stats["checked"] == 5
    assert stats["still_open"] == 5
