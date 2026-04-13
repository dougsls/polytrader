"""CLOB client — trading (orderbook + postOrder).

Wrapper fino em torno de py-clob-client (SDK oficial). O SDK cuida da
assinatura EIP-712 e dos 5 headers L2. Nosso trabalho é:
  1. Aplicar @retry_on_425 nas operações de escrita (Diretiva 2).
  2. Rotear ordens via exchange neg_risk quando MarketSpec.neg_risk=True
     (Diretiva 4).
  3. Consumir OrderDraft já quantizado (Diretiva 1) — nunca construir
     ordem com float cru dentro deste módulo.
"""
from __future__ import annotations

from typing import Any

import httpx
import orjson

from src.api.http import get_http_client
from src.api.order_builder import OrderDraft
from src.api.retry import retry_on_425
from src.core.exceptions import PolymarketAPIError, RateLimitError
from src.core.logger import get_logger

log = get_logger(__name__)

DEFAULT_CLOB_HOST = "https://clob.polymarket.com"


class CLOBClient:
    """Wrapper HTTP do CLOB. Operações públicas (midpoint, book, price)
    usam apenas HTTP; operações autenticadas (postOrder, cancel) delegam
    para py-clob-client que é construído sob demanda.

    Na Fase 2 deixamos o SDK autenticado como injeção opcional — a
    conexão real com chave privada só é exercida no executor.
    """

    def __init__(
        self,
        host: str = DEFAULT_CLOB_HOST,
        *,
        signed_client: Any | None = None,
        neg_risk_signed_client: Any | None = None,
    ) -> None:
        self._host = host.rstrip("/")
        self._signed = signed_client
        self._signed_neg_risk = neg_risk_signed_client

    # ------------------------------------------------------------------ #
    # Público (sem auth)                                                 #
    # ------------------------------------------------------------------ #

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        client = await get_http_client()
        try:
            resp = await client.get(f"{self._host}{path}", params=params)
        except httpx.HTTPError as e:
            raise PolymarketAPIError(f"clob network: {e}", endpoint=path) from e
        if resp.status_code == 429:
            raise RateLimitError(
                "clob rate limit", endpoint=path, status=429,
                retry_after=float(resp.headers.get("retry-after", "1")),
            )
        if resp.status_code == 425:
            resp.raise_for_status()
        if resp.status_code >= 400:
            raise PolymarketAPIError(
                f"clob {resp.status_code}",
                endpoint=path, status=resp.status_code, detail=resp.text[:500],
            )
        return orjson.loads(resp.content)

    @retry_on_425()
    async def midpoint(self, token_id: str) -> float:
        data = await self._get("/midpoint", params={"token_id": token_id})
        return float(data["mid"])

    @retry_on_425()
    async def price(self, token_id: str, side: str) -> float:
        data = await self._get("/price", params={"token_id": token_id, "side": side})
        return float(data["price"])

    @retry_on_425()
    async def book(self, token_id: str) -> dict[str, Any]:
        return await self._get("/book", params={"token_id": token_id})

    # ------------------------------------------------------------------ #
    # Autenticado — requer py-clob-client já instanciado                 #
    # ------------------------------------------------------------------ #

    def _pick_signer(self, draft: OrderDraft) -> Any:
        """Diretiva 4 — ordens em mercado neg_risk roteiam para o adapter
        apropriado. O chamador deve fornecer ambos os clients na injeção.
        """
        if draft.neg_risk:
            if self._signed_neg_risk is None:
                raise RuntimeError(
                    "ordem neg_risk recebida mas neg_risk_signed_client não foi injetado"
                )
            return self._signed_neg_risk
        if self._signed is None:
            raise RuntimeError("signed_client não foi injetado — CLOB em modo read-only")
        return self._signed

    @retry_on_425()
    async def post_order(self, draft: OrderDraft, order_type: str = "GTC") -> dict[str, Any]:
        """Envia ordem pronta para assinatura+post no CLOB.

        Por ora, é um shim: o py-clob-client tem API síncrona. Quando o
        executor for implementado na Fase 3, este método ficará `async
        def` rodando `loop.run_in_executor` para não bloquear o loop.
        """
        signer = self._pick_signer(draft)
        raise NotImplementedError(
            "post_order será implementado na Fase 3 (executor) usando "
            f"signer={type(signer).__name__!r}, order_type={order_type!r}"
        )
