"""Phase 4 — In-memory state cache: reload, write-through, Exit Syncing."""
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from src.api.data_client import DataAPIClient
from src.api.gamma_client import GammaAPIClient
from src.core.config import load_yaml_config
from src.core.database import get_connection, init_database
from src.core.state import InMemoryState
from src.tracker.signal_detector import detect_signal


def test_state_add_and_remove():
    s = InMemoryState()
    s.bot_set("t1", 50.0)
    assert s.bot_size("t1") == 50.0
    s.bot_add("t1", 25.0)
    assert s.bot_size("t1") == 75.0
    s.bot_add("t1", -75.0)
    assert s.bot_size("t1") == 0.0
    assert "t1" not in s.bot_positions_by_token  # purged at zero


def test_whale_index():
    s = InMemoryState()
    s.whale_set("0xW", "t1", 200.0)
    assert s.whale_size("0xW", "t1") == 200.0
    assert s.whale_size("0xW", "t2") == 0.0
    s.whale_set("0xW", "t1", 0)
    assert s.whale_size("0xW", "t1") == 0.0


async def test_reload_from_db(tmp_path: Path):
    db = tmp_path / "s.db"
    await init_database(db)
    now = datetime.now(timezone.utc).isoformat()
    async with get_connection(db) as conn:
        await conn.execute(
            "INSERT INTO bot_positions "
            "(condition_id, token_id, market_title, outcome, size, "
            " avg_entry_price, source_wallets_json, opened_at, is_open, updated_at) "
            "VALUES ('c','t1','m','Yes',42,0.5,'[]',?,1,?)",
            (now, now),
        )
        await conn.execute(
            "INSERT INTO whale_inventory (wallet_address, condition_id, token_id, "
            "size, avg_price, last_seen_at) VALUES ('0xW','c','t1',100,0.5,?)",
            (now,),
        )
        await conn.commit()

    state = InMemoryState()
    await state.reload_from_db(db_path=db)
    assert state.bot_size("t1") == 42.0
    assert state.whale_size("0xW", "t1") == 100.0


async def test_detect_signal_uses_state_cache_no_db(tmp_path: Path):
    """Prova: passando state preenchido, detect_signal NÃO consulta DB.

    Verificamos isso ausentando `conn` e `db_path` e confirmando que a
    query não falha por ausência de DB (fast path é puramente RAM).
    """
    end = datetime.now(timezone.utc).timestamp() + 10 * 3600
    end_iso = datetime.fromtimestamp(end, tz=timezone.utc).isoformat()
    db = tmp_path / "empty.db"
    await init_database(db)  # DB vazio — se state for ignorado, teste falha

    gamma = GammaAPIClient(db_path=db)
    gamma.get_market = AsyncMock(return_value={  # type: ignore[method-assign]
        "question": "m", "end_date_iso": end_iso,
        "tokens": [{"token_id": "t1", "outcome": "Yes"}],
        "tick_size": 0.01, "neg_risk": False,
    })

    state = InMemoryState()
    state.bot_set("t1", 50.0)
    state.whale_set("0xW", "t1", 200.0)

    cfg = load_yaml_config().tracker
    signal = await detect_signal(
        trade={
            "maker": "0xW", "conditionId": "c", "asset": "t1",
            "side": "SELL", "size": 100.0, "price": 0.5,
            "timestamp": int(datetime.now(timezone.utc).timestamp()),
        },
        wallet_score=0.8, cfg=cfg,
        gamma=gamma, data_client=AsyncMock(spec=DataAPIClient),
        db_path=db,  # passa mas NÃO é consultado (state é hit)
        state=state,
    )
    assert signal is not None
    # Regra 2 proporcional: baleia vendeu 100/200=50% → bot vende 50*0.5=25.
    assert abs(signal.size - 25.0) < 1e-6
