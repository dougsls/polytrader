"""Pool de carteiras ativas — persistência + filtro Set para RTDS.

Mantém um `set[str]` de endereços em memória (referência viva usada pelo
RTDSClient — Regra 4) e sincroniza com a tabela `tracked_wallets`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core.config import ScannerConfig
from src.core.database import DEFAULT_DB_PATH, get_connection
from src.core.logger import get_logger
from src.scanner.profiler import WalletProfile
from src.scanner.scorer import score_wallet

log = get_logger(__name__)


class WalletPool:
    """Top-N carteiras ranqueadas por score.

    O `active_addresses` é o Set vivo injetado no RTDSClient. NUNCA
    reatribuir — mutar in-place preserva a referência.
    """

    def __init__(self, cfg: ScannerConfig, db_path: Path = DEFAULT_DB_PATH) -> None:
        self._cfg = cfg
        self._db_path = db_path
        self.active_addresses: set[str] = set()

    def rank(self, profiles: list[WalletProfile]) -> list[tuple[WalletProfile, float]]:
        """Aplica scorer e devolve top N (filtrando score=0)."""
        scored: list[tuple[WalletProfile, float]] = []
        for p in profiles:
            s = score_wallet(p, self._cfg)
            if s > 0.0:
                scored.append((p, s))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[: self._cfg.max_wallets_tracked]

    async def sync(self, ranked: list[tuple[WalletProfile, float]]) -> None:
        """Persiste em `tracked_wallets` e atualiza o Set vivo.

        Carteiras que saíram do top são marcadas is_active=0 (histórico
        fica no DB para auditoria).
        """
        now = datetime.now(timezone.utc).isoformat()
        new_set: set[str] = {p.address for p, _ in ranked}

        async with get_connection(self._db_path) as db:
            # Desativa quem saiu do top
            await db.execute(
                "UPDATE tracked_wallets SET is_active=0, updated_at=? "
                "WHERE is_active=1 AND address NOT IN (" + ",".join(["?"] * len(new_set)) + ")"
                if new_set
                else "UPDATE tracked_wallets SET is_active=0, updated_at=? WHERE is_active=1",
                (now, *new_set) if new_set else (now,),
            )
            # Upsert do top
            for profile, score in ranked:
                await db.execute(
                    """
                    INSERT INTO tracked_wallets
                        (address, name, score, pnl_usd, win_rate, total_trades,
                         tracked_since, is_active, last_trade_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(address) DO UPDATE SET
                        name=excluded.name,
                        score=excluded.score,
                        pnl_usd=excluded.pnl_usd,
                        win_rate=excluded.win_rate,
                        total_trades=excluded.total_trades,
                        is_active=1,
                        last_trade_at=excluded.last_trade_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        profile.address, profile.name, score, profile.pnl_usd,
                        profile.win_rate, profile.total_trades, now,
                        profile.last_trade_at.isoformat() if profile.last_trade_at else None,
                        now,
                    ),
                )
                await db.execute(
                    "INSERT INTO wallet_scores_history "
                    "(wallet_address, score, pnl_usd, win_rate, total_trades, volume_usd, scored_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (profile.address, score, profile.pnl_usd, profile.win_rate,
                     profile.total_trades, profile.volume_usd, now),
                )
            await db.commit()

        # Atualiza Set vivo in-place (preserva identidade para consumidores).
        self.active_addresses.clear()
        self.active_addresses.update(new_set)
        log.info("wallet_pool_synced", active=len(new_set))
