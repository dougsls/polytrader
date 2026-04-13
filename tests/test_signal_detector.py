"""Regra 2 — Exit Syncing: prova do espelhamento rígido de inventário."""
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from src.api.data_client import DataAPIClient
from src.api.gamma_client import GammaAPIClient
from src.core.config import load_yaml_config
from src.core.database import get_connection, init_database
from src.tracker.signal_detector import detect_signal


def _trade(side: str, *, maker: str = "0xWHALE", token: str = "t1",
           cond: str = "0xCOND", size: float = 100.0, price: float = 0.50) -> dict:
    return {
        "maker": maker, "conditionId": cond, "asset": token,
        "side": side, "size": size, "price": price,
        "timestamp": int(datetime.now(timezone.utc).timestamp()),
    }


def _mock_gamma(tmp: Path, hours_ahead: float = 10.0) -> GammaAPIClient:
    end = datetime.now(timezone.utc).timestamp() + hours_ahead * 3600
    end_iso = datetime.fromtimestamp(end, tz=timezone.utc).isoformat()
    gamma = GammaAPIClient(db_path=tmp)
    gamma.get_market = AsyncMock(return_value={  # type: ignore[method-assign]
        "question": "Test market", "end_date_iso": end_iso,
        "tokens": [{"token_id": "t1", "outcome": "Yes"}],
        "tick_size": 0.01, "neg_risk": False,
    })
    return gamma


async def _seed_bot_position(db_path: Path, token_id: str, size: float) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with get_connection(db_path) as db:
        await db.execute(
            "INSERT INTO bot_positions "
            "(condition_id, token_id, market_title, outcome, size, "
            " avg_entry_price, source_wallets_json, opened_at, is_open, updated_at) "
            "VALUES ('0xCOND', ?, 'm', 'Yes', ?, 0.5, '[\"0xWHALE\"]', ?, 1, ?)",
            (token_id, size, now, now),
        )
        await db.commit()


async def _seed_whale(db_path: Path, wallet: str, token_id: str, size: float) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with get_connection(db_path) as db:
        await db.execute(
            "INSERT INTO whale_inventory (wallet_address, condition_id, token_id, "
            "size, avg_price, last_seen_at) VALUES (?, '0xCOND', ?, ?, 0.5, ?)",
            (wallet, token_id, size, now),
        )
        await db.commit()


async def test_sell_without_bot_position_returns_none(tmp_path: Path):
    db = tmp_path / "t.db"
    await init_database(db)
    cfg = load_yaml_config().tracker
    signal = await detect_signal(
        trade=_trade("SELL"), wallet_score=0.8, cfg=cfg,
        gamma=_mock_gamma(db), data_client=AsyncMock(spec=DataAPIClient),
        db_path=db,
    )
    assert signal is None


async def test_sell_with_proportional_size(tmp_path: Path):
    db = tmp_path / "t.db"
    await init_database(db)
    # bot tem 50 tokens; baleia tinha 200 e vende 100 (50% da posição).
    # → bot deve vender 50 * 0.5 = 25 tokens.
    await _seed_bot_position(db, "t1", 50.0)
    await _seed_whale(db, "0xWHALE", "t1", 200.0)

    cfg = load_yaml_config().tracker
    signal = await detect_signal(
        trade=_trade("SELL", size=100.0), wallet_score=0.8, cfg=cfg,
        gamma=_mock_gamma(db), data_client=AsyncMock(spec=DataAPIClient),
        db_path=db,
    )
    assert signal is not None
    assert signal.side == "SELL"
    assert abs(signal.size - 25.0) < 1e-6


async def test_buy_passes_without_position(tmp_path: Path):
    db = tmp_path / "t.db"
    await init_database(db)
    cfg = load_yaml_config().tracker
    signal = await detect_signal(
        trade=_trade("BUY"), wallet_score=0.8, cfg=cfg,
        gamma=_mock_gamma(db), data_client=AsyncMock(spec=DataAPIClient),
        db_path=db,
    )
    assert signal is not None
    assert signal.side == "BUY"
    assert signal.size == 100.0  # tamanho cru


async def test_hard_block_long_market(tmp_path: Path):
    db = tmp_path / "t.db"
    await init_database(db)
    cfg = load_yaml_config().tracker
    signal = await detect_signal(
        trade=_trade("BUY"), wallet_score=0.8, cfg=cfg,
        gamma=_mock_gamma(db, hours_ahead=24 * 10),  # 10 dias > hard_block_days=3
        data_client=AsyncMock(spec=DataAPIClient),
        db_path=db,
    )
    assert signal is None


async def test_too_close_to_resolution(tmp_path: Path):
    db = tmp_path / "t.db"
    await init_database(db)
    cfg = load_yaml_config().tracker
    signal = await detect_signal(
        trade=_trade("BUY"), wallet_score=0.8, cfg=cfg,
        gamma=_mock_gamma(db, hours_ahead=0.05),  # 3min < min_hours 0.1
        data_client=AsyncMock(spec=DataAPIClient),
        db_path=db,
    )
    assert signal is None
