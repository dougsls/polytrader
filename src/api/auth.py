"""Phase 3/5 — EOA auth prefetch.

A Polymarket CLOB exige credenciais L2 (API key + secret + passphrase)
derivadas via assinatura EIP-712 da chave privada (L1). A negociação
custa 1-2 RTT NY→London (~100-300ms) e acontece em:
    POST /auth/api-key (ou deriva via create_or_derive_api_creds)

Este módulo faz a derivação **uma vez no startup** e retorna dois
`py-clob-client` signers: um para o CTF padrão, outro para o neg_risk
adapter. O CLOBClient já sabe rotear ordens entre eles (Diretiva 4).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from src.core.logger import get_logger

log = get_logger(__name__)

POLYGON_CHAIN_ID = 137


@dataclass(frozen=True, slots=True)
class L2Credentials:
    api_key: str
    secret: str
    passphrase: str
    address: str


@dataclass(frozen=True, slots=True)
class SignedClients:
    """Dois clients py-clob-client prontos para postOrder.

    standard: exchange padrão (1000 mercados binários).
    neg_risk: adapter para mercados multi-outcome (Diretiva 4).
    """

    standard: Any
    neg_risk: Any
    credentials: L2Credentials


async def prefetch_credentials(
    *,
    private_key: str,
    funder: str,
    signature_type: int,
    host: str = "https://clob.polymarket.com",
) -> SignedClients:
    """Constrói os 2 signers e cacheia as credenciais L2 em memória.

    Roda em executor pois py-clob-client é síncrono (requests + eth_account).
    """
    if not private_key:
        raise RuntimeError(
            "PRIVATE_KEY vazio — configure .env antes de chamar prefetch_credentials."
        )

    def _build() -> SignedClients:
        # Imports lazy pra manter o módulo carregável sem py-clob-client em dev.
        from py_clob_client.client import ClobClient  # type: ignore[import-not-found]
        from py_clob_client.constants import POLYGON  # type: ignore[import-not-found]

        chain = POLYGON if POLYGON == POLYGON_CHAIN_ID else POLYGON_CHAIN_ID

        # Standard signer
        std = ClobClient(
            host=host, key=private_key, chain_id=chain,
            funder=funder, signature_type=signature_type,
        )
        creds = std.create_or_derive_api_creds()
        std.set_api_creds(creds)

        # neg_risk signer: mesma chave, mesmo adapter do py-clob-client.
        # Em produção, a lib 0.18+ aceita flag neg_risk nas chamadas;
        # mantemos o mesmo client — o CLOBClient roteia via order args.
        nr = std

        return SignedClients(
            standard=std,
            neg_risk=nr,
            credentials=L2Credentials(
                api_key=str(getattr(creds, "api_key", "")),
                secret=str(getattr(creds, "api_secret", "")),
                passphrase=str(getattr(creds, "api_passphrase", "")),
                address=funder or "",
            ),
        )

    log.info("eoa_auth_prefetch_start", signature_type=signature_type)
    loop = asyncio.get_running_loop()
    signed = await loop.run_in_executor(None, _build)
    log.info(
        "eoa_auth_prefetch_done",
        address=signed.credentials.address[:10] + "…" if signed.credentials.address else "",
    )
    return signed
