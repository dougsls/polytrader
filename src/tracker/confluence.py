"""⚠️ ALPHA — Confluence tracker (consensus amplification).

Quando 2+ baleias do nosso pool convergem no MESMO lado (BUY YES) do
MESMO mercado em janela curta (15min), isso é sinal de convicção
extrema — múltiplas mentes lendo o mesmo edge. Este tracker deteta
confluência em RAM e o `RiskManager` amplifica o sizing proporcional
ao count.

Sem isso, cada trade vinha sized isoladamente, ignorando o sinal de
consenso. Alpha puro desperdiçado.

Estrutura:
    `dict[(condition_id, side), dict[wallet_lower, monotonic_ts]]`
    - Chave por (mercado, lado) → mapa de wallets distintas e quando
      observamos cada uma. Wallet_lower normaliza case.
    - Cleanup automático em cada `record()`: drop wallets fora da janela.
    - Drop entry inteira quando set fica vazio (memory hygiene).

API:
    `tracker.record(condition_id, side, wallet) -> int`
    Retorna o count de wallets distintas dentro da janela INCLUINDO a
    chamada atual. count=1 = primeira whale, count=2+ = confluência.
"""
from __future__ import annotations

import time
from typing import Literal

DEFAULT_WINDOW_SECONDS = 15 * 60  # 15 minutos


class ConfluenceTracker:
    """In-RAM rolling window de wallets que convergem por (cid, side)."""

    def __init__(self, window_seconds: float = DEFAULT_WINDOW_SECONDS) -> None:
        self._window = window_seconds
        # (condition_id, side) → {wallet_lower: monotonic_ts}
        self._wallets_by_key: dict[
            tuple[str, Literal["BUY", "SELL"]], dict[str, float]
        ] = {}

    def record(
        self,
        condition_id: str,
        side: Literal["BUY", "SELL"],
        wallet: str,
    ) -> int:
        """Registra observação e retorna count de wallets distintas
        dentro da janela.

        - Adiciona/atualiza ts da wallet.
        - Drop wallets cujo último ts < (now - window).
        - Drop entry inteira se ficou vazia (não acontece aqui pois
          acabamos de adicionar; cleanup happens em re-records futuros).
        """
        now = time.monotonic()
        cutoff = now - self._window
        key: tuple[str, Literal["BUY", "SELL"]] = (condition_id, side)
        wallets = self._wallets_by_key.setdefault(key, {})
        # Atualiza/insere a wallet atual.
        wallets[wallet.lower()] = now
        # Cleanup: drop expired wallets neste bucket.
        expired = [w for w, ts in wallets.items() if ts < cutoff]
        for w in expired:
            wallets.pop(w, None)
        return len(wallets)

    def count(
        self,
        condition_id: str,
        side: Literal["BUY", "SELL"],
    ) -> int:
        """Conta wallets distintas no bucket (sem registrar nova).

        Útil para inspeção/dashboard. Aplica cleanup mas não insere.
        """
        now = time.monotonic()
        cutoff = now - self._window
        key: tuple[str, Literal["BUY", "SELL"]] = (condition_id, side)
        wallets = self._wallets_by_key.get(key)
        if not wallets:
            return 0
        expired = [w for w, ts in wallets.items() if ts < cutoff]
        for w in expired:
            wallets.pop(w, None)
        if not wallets:
            self._wallets_by_key.pop(key, None)
            return 0
        return len(wallets)

    def gc(self) -> int:
        """Garbage collect — drop entries totalmente expiradas. Retorna
        nº de entries removidas. Pode ser chamado periodicamente em
        loops longos para memory hygiene."""
        now = time.monotonic()
        cutoff = now - self._window
        removed = 0
        for key in list(self._wallets_by_key.keys()):
            wallets = self._wallets_by_key[key]
            expired = [w for w, ts in wallets.items() if ts < cutoff]
            for w in expired:
                wallets.pop(w, None)
            if not wallets:
                self._wallets_by_key.pop(key, None)
                removed += 1
        return removed
