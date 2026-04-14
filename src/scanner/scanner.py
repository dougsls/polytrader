"""Scanner — loop periódico que mantém o pool de carteiras vivo.

Fluxo por ciclo (default 60min):
    1. fetch_candidates(leaderboard de 7d + 30d) → WalletProfiles
    2. WalletPool.rank(profiles) → top-N (aplica Regra 3 wash filter)
    3. WalletPool.sync(ranked) → persiste + muta active_wallets IN PLACE
    4. snapshot_whale(data_client, addr, state) para carteiras NOVAS
       → popula whale_inventory em RAM para habilitar Regra 2 (Exit Syncing)
    5. Atualiza wallet_scores dict IN PLACE (consumido pelo TradeMonitor)

Design: `active_wallets` e `wallet_scores` são **referências vivas** compartilhadas
com RTDSClient e TradeMonitor. NUNCA reatribuir — mutação in-place preserva
identidade do objeto.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from src.api.data_client import DataAPIClient
from src.core.config import ScannerConfig
from src.core.logger import get_logger
from src.core.state import InMemoryState
from src.scanner.leaderboard import fetch_candidates
from src.scanner.profiler import WalletProfile
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
    ) -> None:
        self._cfg = cfg
        self._data = data_client
        self._pool = pool
        self._active = active_wallets  # referência viva — NUNCA reatribuir
        self._scores = wallet_scores   # idem
        self._state = state
        self._stop = asyncio.Event()

    def _profiles_from_whitelist(self) -> list[WalletProfile]:
        """Gera WalletProfile sintéticos de endereços manuais.

        Usado quando o leaderboard da Polymarket Data API está indisponível
        (ex: endpoint removido em 2026). Scores são constantes 1.0 — não
        passam pelos gates do scorer. O pool trata como top ranqueado.
        """
        now = datetime.now(timezone.utc)
        return [
            WalletProfile(
                address=addr, name=None,
                pnl_usd=10_000.0, volume_usd=50_000.0,
                win_rate=0.70, total_trades=50, distinct_markets=10,
                short_term_trade_ratio=0.80,
                last_trade_at=now - timedelta(hours=1),
            )
            for addr in self._cfg.manual_whitelist
        ]

    async def tick(self) -> None:
        """Um ciclo de scan — útil pra chamar no startup + no loop."""
        try:
            profiles = await fetch_candidates(
                self._data,
                periods=self._cfg.leaderboard_periods,
                short_term_threshold_hours=self._cfg.short_term_threshold_hours,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("scanner_fetch_failed", err=repr(exc))
            profiles = []

        # Fallback — merge whitelist manual quando leaderboard vazia/falha.
        if not profiles and self._cfg.manual_whitelist:
            log.info(
                "scanner_using_manual_whitelist",
                count=len(self._cfg.manual_whitelist),
            )
            profiles = self._profiles_from_whitelist()

        ranked = self._pool.rank(profiles)

        # Safety: Rate-limit ou erro transiente zera `profiles`. Se
        # fizermos sync com ranked=[], TODAS as carteiras ativas viram
        # is_active=0 e o bot perde 1h de operação. Preserva pool atual.
        if not ranked and not profiles:
            log.warning("scanner_empty_result_skipping_sync", active=len(self._active))
            return

        previous = set(self._active)
        await self._pool.sync(ranked)  # muta self._pool.active_addresses in-place

        # --- Propaga para os dicts vivos (in-place, preserva identidade) ---
        new_addresses = self._pool.active_addresses
        self._active.clear()
        self._active.update(new_addresses)

        self._scores.clear()
        for profile, score in ranked:
            self._scores[profile.address] = score

        # --- Snapshot de posições para carteiras NOVAS (Regra 2 warm-up) ---
        # Em paralelo: 20 carteiras × 200ms NY→London em série = 4s bloqueando
        # o primeiro tick. `return_exceptions=True` garante que uma falha não
        # aborta o resto.
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
            newly_added=len(newly_added),
        )

    async def run_loop(self) -> None:
        """Loop principal — executa um tick inicial + ticks a cada N min."""
        await self.tick()
        interval = self._cfg.scan_interval_minutes * 60
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                await self.tick()

    async def stop(self) -> None:
        self._stop.set()
