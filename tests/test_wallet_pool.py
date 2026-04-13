"""WalletPool — ranking + Set vivo preservado in-place."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.config import load_yaml_config
from src.core.database import get_connection, init_database
from src.scanner.profiler import WalletProfile
from src.scanner.wallet_pool import WalletPool


def _p(address: str, pnl: float, volume: float = None) -> WalletProfile:
    return WalletProfile(
        address=address, name=address[-3:],
        pnl_usd=pnl,
        volume_usd=volume if volume is not None else pnl * 4,
        win_rate=0.7, total_trades=50, distinct_markets=8,
        short_term_trade_ratio=0.8,
        last_trade_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )


def test_rank_filters_and_sorts():
    cfg = load_yaml_config().scanner
    pool = WalletPool(cfg)
    profiles = [_p("0xA", 1_000), _p("0xB", 5_000), _p("0xWASH", 100, volume=100_000)]
    ranked = pool.rank(profiles)
    assert len(ranked) == 2  # wash excluded
    assert ranked[0][0].address == "0xB"  # highest PnL first
    assert ranked[0][1] > ranked[1][1]


async def test_sync_mutates_set_in_place(tmp_path: Path):
    db = tmp_path / "t.db"
    await init_database(db)
    cfg = load_yaml_config().scanner
    pool = WalletPool(cfg, db_path=db)
    # Referência externa que será compartilhada com o RTDSClient
    external_ref = pool.active_addresses

    ranked = pool.rank([_p("0xA", 2_000), _p("0xB", 3_000)])
    await pool.sync(ranked)

    # Identidade preservada (mesmo objeto), conteúdo atualizado.
    assert pool.active_addresses is external_ref
    assert external_ref == {"0xA", "0xB"}

    # Sync novamente com carteiras diferentes: o Set externo reflete.
    ranked2 = pool.rank([_p("0xC", 4_000)])
    await pool.sync(ranked2)
    assert pool.active_addresses is external_ref
    assert external_ref == {"0xC"}

    # Verifica persistência: carteiras antigas marcadas is_active=0.
    async with get_connection(db) as conn:
        async with conn.execute(
            "SELECT address, is_active FROM tracked_wallets ORDER BY address"
        ) as cur:
            rows = await cur.fetchall()
    status = {r["address"]: r["is_active"] for r in rows}
    assert status["0xA"] == 0
    assert status["0xB"] == 0
    assert status["0xC"] == 1
