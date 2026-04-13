"""Consulta e agrega o leaderboard em WalletProfile pronto para scoring."""
from __future__ import annotations

from src.api.data_client import DataAPIClient
from src.core.logger import get_logger
from src.scanner.profiler import WalletProfile, profile_from_leaderboard_entry

log = get_logger(__name__)


async def fetch_candidates(
    client: DataAPIClient,
    *,
    periods: list[str],
    limit: int = 50,
    short_term_threshold_hours: int = 48,
) -> list[WalletProfile]:
    """Agrega leaderboards de múltiplos períodos, deduplicando por address.

    Quando a mesma carteira aparece em 7d e 30d, guardamos o maior pnl_usd
    como âncora (consistência = aparecer em múltiplos períodos).
    """
    merged: dict[str, WalletProfile] = {}
    for period in periods:
        try:
            rows = await client.leaderboard(period=period, limit=limit)
        except Exception as exc:  # noqa: BLE001
            log.warning("leaderboard_fetch_failed", period=period, err=repr(exc))
            continue
        for row in rows:
            try:
                profile = profile_from_leaderboard_entry(
                    row, short_term_threshold_hours=short_term_threshold_hours
                )
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("leaderboard_row_skipped", err=repr(exc))
                continue
            existing = merged.get(profile.address)
            if existing is None or profile.pnl_usd > existing.pnl_usd:
                merged[profile.address] = profile
    return list(merged.values())
