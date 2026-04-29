"""CLOB client — trading (orderbook + postOrder), async-native HFT.

Wrapper fino em torno de py-clob-client (SDK oficial). O SDK cuida da
assinatura EIP-712 (puro CPU, ~5ms) e da derivação de credenciais L2.
Nosso trabalho:
  1. Diretiva 2 — `@retry_on_425` nas operações de escrita.
  2. Diretiva 4 — Rotear ordens via exchange neg_risk quando
     `MarketSpec.neg_risk=True`.
  3. Diretiva 1 — Consumir `OrderDraft` já quantizado.
  4. **HFT post_order**: Submissão **100% async via httpx**. O SDK
     fornece o payload assinado (CPU-bound), mas o POST HTTP NÃO passa
     por `requests`/`run_in_executor` — vai direto pelo nosso
     `httpx.AsyncClient` singleton (HTTP/2 keep-alive). Isso elimina
     o gargalo de threading em batches de 10+ ordens.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import orjson

from src.api.clob_l2_auth import build_l2_headers
from src.api.http import get_http_client
from src.api.order_builder import OrderDraft
from src.api.retry import retry_on_425
from src.core.exceptions import PolymarketAPIError, RateLimitError
from src.core.logger import get_logger

log = get_logger(__name__)

DEFAULT_CLOB_HOST = "https://clob.polymarket.com"
POST_ORDER_PATH = "/order"


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
        credentials: Any | None = None,  # L2Credentials (avoid circular import)
    ) -> None:
        self._host = host.rstrip("/")
        self._signed = signed_client
        self._signed_neg_risk = neg_risk_signed_client
        # HFT — credenciais L2 são imutáveis após startup; cacheadas em RAM.
        # Sem creds o post_order cai em NotImplementedError (read-only mode).
        self._creds = credentials

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

    async def maker_price(
        self,
        token_id: str,
        side: str,
        tick_size: float,
        offset_ticks: int = 1,
    ) -> float:
        """Calcula um preço MAKER (não-crossing) a partir do book.

        BUY: pega best_bid e SOBE em N ticks (mas nunca >= best_ask).
        SELL: pega best_ask e DESCE em N ticks (mas nunca <= best_bid).

        Retorna 0.0 se book vazio. Caller deve tratar como "não-tradeável".
        Track B: usado pelo executor pra postar GTC dentro do spread sem
        cruzar — captura o spread em vez de pagar taker.
        """
        try:
            book = await self.book(token_id)
        except PolymarketAPIError:
            return 0.0
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        best_bid = float(bids[0]["price"]) if bids else 0.0
        best_ask = float(asks[0]["price"]) if asks else 1.0
        side_u = side.upper()
        if side_u == "BUY":
            target = best_bid + offset_ticks * tick_size
            # Não cruzar — preço maker BUY < best_ask
            if best_ask > 0 and target >= best_ask:
                target = max(best_bid, best_ask - tick_size)
            return round(target / tick_size) * tick_size
        # SELL
        target = best_ask - offset_ticks * tick_size
        if target <= best_bid:
            target = min(best_ask, best_bid + tick_size)
        return round(target / tick_size) * tick_size

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
    async def post_order(
        self, draft: OrderDraft, order_type: str = "GTC",
    ) -> dict[str, Any]:
        """HFT — Assina inline (CPU ~5ms) e POST via httpx async.

        ANTES: SDK síncrono em `run_in_executor` → thread context switch
        + GIL contention + `requests` blocking ~100ms NY→London.
        AGORA: signing inline (CPU puro) + `httpx.AsyncClient` HTTP/2
        keep-alive multiplexado. Em batches de 10 ordens em paralelo,
        a economia é ~80-100ms × 10 = 800ms-1s cumulativos.

        Pipeline:
            1. SDK.create_order(args, options) → SignedOrder dataclass
               (puro CPU: keccak + ECDSA via coincurve C bindings, ~5ms)
            2. order_to_json → dict {order, owner, orderType, postOnly}
            3. json.dumps(separators=(",", ":")) → string canônica
            4. build_l2_headers (HMAC SHA256, ~1µs)
            5. httpx.AsyncClient.post(host + /order, headers, content=body)
        """
        if self._creds is None:
            raise NotImplementedError(
                "CLOB read-only — credentials L2 não foram injetadas. "
                "Certifique-se que prefetch_credentials() rodou no startup."
            )

        signer = self._pick_signer(draft)

        # Imports lazy — manter módulo importável sem py-clob-client em dev.
        try:
            from py_clob_client.clob_types import (  # type: ignore[import-not-found]
                OrderArgs,
                OrderType,
                PartialCreateOrderOptions,
            )
            from py_clob_client.utilities import order_to_json  # type: ignore[import-not-found]
        except ImportError as e:
            raise NotImplementedError(
                "py-clob-client não disponível. Instale com `uv sync`."
            ) from e

        # 1. Build signed order — CPU puro, ~5ms (keccak + ECDSA C bindings).
        # Aceitável inline no event loop: bem menor que RTT que evitamos.
        args = OrderArgs(
            token_id=draft.token_id,
            price=float(draft.price),
            size=float(draft.size),
            side=draft.side,
        )
        options = PartialCreateOrderOptions(
            neg_risk=draft.neg_risk,
            tick_size=str(draft.tick_size),
        )
        ot_enum = getattr(OrderType, order_type, OrderType.GTC)
        # SDK extrai string ("GTC", "FOK", etc.) do enum.
        ot_str = ot_enum.value if hasattr(ot_enum, "value") else str(ot_enum)
        signed_order = signer.create_order(args, options=options)

        # 2. Payload canonical JSON (separators sem espaço — exigência do HMAC).
        body_dict = order_to_json(
            signed_order, self._creds.api_key, ot_str, False,  # post_only=False
        )
        body_json = json.dumps(body_dict, separators=(",", ":"), ensure_ascii=False)

        # 3. L2 headers HMAC — ~1µs.
        headers = build_l2_headers(
            address=self._creds.address,
            api_key=self._creds.api_key,
            secret=self._creds.secret,
            passphrase=self._creds.passphrase,
            method="POST",
            path=POST_ORDER_PATH,
            body_json=body_json,
        )
        headers["Content-Type"] = "application/json"

        # 4. POST via httpx.AsyncClient — HTTP/2 keep-alive, sem block.
        client = await get_http_client()
        try:
            resp = await client.post(
                f"{self._host}{POST_ORDER_PATH}",
                headers=headers, content=body_json,
            )
        except httpx.HTTPError as exc:
            raise PolymarketAPIError(
                f"clob network: {exc}", endpoint=POST_ORDER_PATH,
            ) from exc

        if resp.status_code == 429:
            raise RateLimitError(
                "clob rate limit", endpoint=POST_ORDER_PATH, status=429,
                retry_after=float(resp.headers.get("retry-after", "1")),
            )
        if resp.status_code == 425:
            resp.raise_for_status()
        if resp.status_code >= 400:
            raise PolymarketAPIError(
                f"clob {resp.status_code}",
                endpoint=POST_ORDER_PATH, status=resp.status_code,
                detail=resp.text[:500],
            )

        try:
            data: dict[str, Any] = orjson.loads(resp.content)
        except orjson.JSONDecodeError as exc:
            raise PolymarketAPIError(
                f"clob malformed response: {resp.text[:200]}",
                endpoint=POST_ORDER_PATH, status=resp.status_code,
            ) from exc

        # C1 — valida resposta. SDK retorna success/errorMsg/orderID.
        if not data.get("success", False) or data.get("errorMsg"):
            raise PolymarketAPIError(
                f"CLOB rejected: {data.get('errorMsg') or data}",
                endpoint=POST_ORDER_PATH, status=400, detail=str(data)[:500],
            )
        return data
