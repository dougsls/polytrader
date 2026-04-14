"""Orphan recovery — VPS crash mid-fill não quebra o Exit Syncing."""
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from src.api.data_client import DataAPIClient
from src.core.database import get_connection, init_database
from src.core.state import InMemoryState
from src.executor.position_sync import reconcile_bot_positions


async def _seed_db_position(db: Path, token_id: str, size: float) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with get_connection(db) as conn:
        await conn.execute(
            "INSERT INTO bot_positions "
            "(condition_id, token_id, market_title, outcome, size, "
            " avg_entry_price, source_wallets_json, opened_at, is_open, updated_at) "
            "VALUES ('c', ?, 'm', 'Yes', ?, 0.5, '[]', ?, 1, ?)",
            (token_id, size, now, now),
        )
        await conn.commit()


async def test_orphan_recovery_adds_missing_position(tmp_path: Path):
    """DB vazio, mas on-chain tem 42 tokens → adiciona."""
    db = tmp_path / "s.db"
    await init_database(db)

    data = AsyncMock(spec=DataAPIClient)
    data.positions = AsyncMock(return_value=[
        {"asset": "tokA", "conditionId": "c1",
         "size": 42.0, "outcome": "Yes", "avgPrice": 0.55},
    ])

    state = InMemoryState()
    stats = await reconcile_bot_positions(
        data_client=data, wallet_address="0xFUNDER", state=state, db_path=db,
    )

    assert stats == {"added": 1, "updated": 0, "closed": 0, "ignored": 0}
    assert state.bot_size("tokA") == 42.0


async def test_orphan_recovery_resizes_stale_position(tmp_path: Path):
    """DB diz 10, on-chain diz 25 → UPDATE para 25."""
    db = tmp_path / "s.db"
    await init_database(db)
    await _seed_db_position(db, "tokB", 10.0)

    data = AsyncMock(spec=DataAPIClient)
    data.positions = AsyncMock(return_value=[
        {"asset": "tokB", "conditionId": "c", "size": 25.0,
         "outcome": "Yes", "avgPrice": 0.5},
    ])

    state = InMemoryState()
    stats = await reconcile_bot_positions(
        data_client=data, wallet_address="0xFUNDER", state=state, db_path=db,
    )
    assert stats["updated"] == 1
    assert state.bot_size("tokB") == 25.0


async def test_phantom_position_closed(tmp_path: Path):
    """DB tem posição aberta, on-chain não → fecha (is_open=0)."""
    db = tmp_path / "s.db"
    await init_database(db)
    await _seed_db_position(db, "tokGHOST", 99.0)

    data = AsyncMock(spec=DataAPIClient)
    data.positions = AsyncMock(return_value=[])  # nada on-chain

    state = InMemoryState()
    state.bot_set("tokGHOST", 99.0)  # estado defasado também

    stats = await reconcile_bot_positions(
        data_client=data, wallet_address="0xFUNDER", state=state, db_path=db,
    )
    assert stats["closed"] == 1
    assert state.bot_size("tokGHOST") == 0.0


async def test_no_wallet_skips(tmp_path: Path):
    db = tmp_path / "s.db"
    await init_database(db)
    data = AsyncMock(spec=DataAPIClient)
    stats = await reconcile_bot_positions(
        data_client=data, wallet_address="", db_path=db,
    )
    assert stats == {"added": 0, "updated": 0, "closed": 0, "ignored": 0}
    data.positions.assert_not_called()
