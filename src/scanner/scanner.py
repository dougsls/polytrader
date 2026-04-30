"""Scanner — mantém o pool de whales alvo.

Desde 2026-04, Polymarket removeu o endpoint `/leaderboard`. O Scanner
opera em modo **static whitelist** — lê TARGET_WHALES de
`src/scanner/static_whales.py` e mergeia com `config.yaml:
scanner.manual_whitelist` (para overrides do operador).

Fluxo por ciclo:
    1. Carrega 22 whales hardcoded + manual_whitelist (config)
    2. WalletPool.sync → persiste em tracked_wallets, muta active_wallets
    3. snapshot_whale em paralelo para cada whale nova (Regra 2 warm-up)

`active_wallets` e `wallet_scores` são mutados **in-place** para manter
identidade da referência usada pelo RTDSClient e TradeMonitor.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from src.api.data_client import DataAPIClient
from src.core.config import ScannerConfig
from src.core.logger import get_logger
from src.core.state import InMemoryState
from src.scanner.enrich import enrich_profiles
from src.scanner.profiler import WalletProfile
from src.scanner.static_whales import static_whale_profiles
from src.scanner.wallet_pool import WalletPool
from src.tracker.whale_inventory import snapshot_whale

log = get_logger(__name__)


class Scanner:
    def __init__(
        self,
        *,
        cfg: ScannerConfig,
        data_client: DataAPIClient,
        pool: WalletPool,
        active_wallets: set[str],
        wallet_scores: dict[str, float],
        state: InMemoryState,
        wallet_portfolios: dict[str, float] | None = None,
        wallet_win_rates: dict[str, float] | None = None,
    ) -> None:
        self._cfg = cfg
        self._data = data_client
        self._pool = pool
        self._active = active_wallets
        self._scores = wallet_scores
        # Dict vivo address→volume_usd (portfolio da whale via /value).
        # Consumido pelo TradeMonitor pra preencher signal.whale_portfolio_usd.
        self._portfolios = wallet_portfolios if wallet_portfolios is not None else {}
        # RISK MGMT — win rate puro (não composta) por wallet. Usado pelo
        # Kelly Criterion no executor; mantido separado de `wallet_scores`
        # para evitar distorção de p na fórmula f* = p - q/odds.
        self._win_rates = wallet_win_rates if wallet_win_rates is not None else {}
        self._state = state
        self._stop = asyncio.Event()

    def _profiles_from_manual_whitelist(self) -> list[WalletProfile]:
        """Merge: addresses extras vindos de config.yaml.manual_whitelist."""
        now = datetime.now(timezone.utc)
        return [
            WalletProfile(
                address=addr.lower(), name=None,
                pnl_usd=50_000.0, volume_usd=250_000.0,
                win_rate=0.70, total_trades=100, distinct_markets=15,
                short_term_trade_ratio=0.80,
                last_trade_at=now - timedelta(hours=1),
            )
            for addr in self._cfg.manual_whitelist
        ]

    async def tick(self) -> None:
        """Um ciclo de scan — sempre sincroniza a static whitelist."""
        profiles: list[WalletProfile] = list(static_whale_profiles())

        # Mergeia manual_whitelist (se o operador adicionar addresses extras),
        # deduplicando por address lowercase.
        if self._cfg.manual_whitelist:
            seen = {p.address for p in profiles}
            for extra in self._profiles_from_manual_whitelist():
                if extra.address not in seen:
                    profiles.append(extra)
                    seen.add(extra.address)

        # Enriquece cada profile com métricas REAIS da Data API.
        # Substitui os valores sintéticos (100k/0.75/200) por pnl/win_rate/
        # volume reais vindos de /value + /traded + /closed-positions.
        profiles = await enrich_profiles(self._data, profiles)

        ranked = self._pool.rank(profiles)

        previous = set(self._active)
        await self._pool.sync(ranked)  # muta self._pool.active_addresses

        # --- Propaga para as referências vivas (in-place) ---
        new_addresses = self._pool.active_addresses
        self._active.clear()
        self._active.update(new_addresses)

        self._scores.clear()
        self._portfolios.clear()
        self._win_rates.clear()
        for profile, score in ranked:
            self._scores[profile.address] = score
            self._portfolios[profile.address] = profile.volume_usd
            # RISK MGMT — win_rate cru, não composta. Kelly precisa disso.
            self._win_rates[profile.address] = profile.win_rate

        # --- Snapshot paralelo de posições para whales novas ---
        newly_added = new_addresses - previous
        if newly_added:
            results = await asyncio.gather(
                *[snapshot_whale(self._data, a, state=self._state) for a in newly_added],
                return_exceptions=True,
            )
            for addr, res in zip(newly_added, results, strict=False):
                if isinstance(res, Exception):
                    log.warning(
                        "whale_snapshot_failed", addr=addr[:12], err=repr(res),
                    )

        log.info(
            "scanner_tick_done",
            ranked=len(ranked), active=len(new_addresses),
            newly_added=len(newly_added), source="static_whitelist",
        )

    async def run_loop(self) -> None:
        """Loop principal — tick inicial + ticks periódicos."""
        await self.tick()
        interval = self._cfg.scan_interval_minutes * 60
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                await self.tick()

    async def stop(self) -> None:
        self._stop.set()
