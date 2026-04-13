"""Phase 3 — EOA auth prefetch.

A Polymarket CLOB exige credenciais L2 (API key + secret + passphrase)
derivadas via assinatura EIP-712 da chave privada (L1). A negociação
custa 1-2 RTT NY→London (~100-300ms) e acontece em:
    POST /auth/api-key

Este módulo faz a derivação **uma vez no startup** e mantém as
credenciais em RAM. Sinais subsequentes reutilizam o HMAC cacheado —
zero negotiation delay no hot path.

Uso em main.py (após a carteira estar configurada):
    creds = await prefetch_credentials(private_key, funder, sig_type)
    clob = CLOBClient(signed_client=build_signer(creds))
"""
from __future__ import annotations

from dataclasses import dataclass

from src.core.logger import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class L2Credentials:
    """Credenciais derivadas uma única vez. Mantidas em RAM.

    Attributes:
        api_key: `POLY_API_KEY` header em toda request autenticada.
        secret: usado para HMAC-SHA256 do payload (`POLY_SIGNATURE`).
        passphrase: `POLY_PASSPHRASE` header.
        address: endereço da carteira associada.
    """

    api_key: str
    secret: str
    passphrase: str
    address: str


async def prefetch_credentials(
    private_key: str,  # noqa: ARG001 — consumido pelo SDK quando habilitado
    funder: str,  # noqa: ARG001
    signature_type: int,  # noqa: ARG001
) -> L2Credentials:
    """Negocia credenciais L2 via py-clob-client e cacheia em memória.

    Integração real requer `py-clob-client.create_or_derive_api_creds()`
    que é síncrono — envolve em `run_in_executor` para não bloquear o loop.
    Por ora é placeholder; ativado quando o operador injetar chave privada
    em produção (Fase 5 do master plan).
    """
    if not private_key:
        log.warning("eoa_auth_skipped", reason="no_private_key")
        raise RuntimeError(
            "PRIVATE_KEY vazio no .env — impossível derivar credenciais L2. "
            "Configure o .env e reinicie. Em modo paper isso é esperado."
        )
    raise NotImplementedError(
        "EOA auth prefetch — implementação real exige py-clob-client com "
        "ClobClient(host, key, funder, signature_type).create_or_derive_api_creds(). "
        "Será plugado na Fase 5 (go-live) quando operador prover chave."
    )
