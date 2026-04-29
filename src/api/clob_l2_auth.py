"""HFT — L2 HMAC headers para o Polymarket CLOB, async-native.

A Polymarket exige 6 headers para POST/DELETE em endpoints autenticados:
    POLY_ADDRESS    — endereço da wallet (signer)
    POLY_API_KEY    — api_key derivada via prefetch L1
    POLY_PASSPHRASE — passphrase derivada via prefetch L1
    POLY_TIMESTAMP  — Unix timestamp atual (segundos)
    POLY_SIGNATURE  — base64url(HMAC_SHA256(secret_decoded, msg))
                       msg = timestamp + method + path + body_json

Implementação 1:1 com `py_clob_client/signing/hmac.py` e
`py_clob_client/headers/headers.py::create_level_2_headers`. A SDK
oficial usa `requests` síncrono que entope o event loop em batches HFT;
queremos os MESMOS headers porém computados puro CPU (sem I/O).

`build_l2_headers` é determinístico, ~1µs por chamada — cabe no hot
path sem `run_in_executor`. POST roda via `httpx.AsyncClient`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Final


def _hmac_signature(
    secret_b64url: str,
    timestamp: int,
    method: str,
    request_path: str,
    body_json: str | None,
) -> str:
    """Reproduz py_clob_client/signing/hmac.py::build_hmac_signature.

    A SDK oficial faz `str(body).replace("'", '"')` para emparelhar
    com a serialização Go/TS. Como aqui já passamos o body como JSON
    string canônico (`json.dumps`), o replace é no-op — mas mantemos
    a chamada por paridade defensiva.
    """
    decoded_secret = base64.urlsafe_b64decode(secret_b64url)
    message = f"{timestamp}{method}{request_path}"
    if body_json:
        # Replace por paridade com a SDK; em payload já-canônico não muda nada.
        message += body_json.replace("'", '"')
    digest = hmac.new(
        decoded_secret, message.encode("utf-8"), hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8")


# Constantes de header (literais, baixa-cardinalidade).
POLY_ADDRESS: Final = "POLY_ADDRESS"
POLY_SIGNATURE: Final = "POLY_SIGNATURE"
POLY_TIMESTAMP: Final = "POLY_TIMESTAMP"
POLY_API_KEY: Final = "POLY_API_KEY"
POLY_PASSPHRASE: Final = "POLY_PASSPHRASE"


def build_l2_headers(
    *,
    address: str,
    api_key: str,
    secret: str,
    passphrase: str,
    method: str,
    path: str,
    body_json: str | None,
) -> dict[str, str]:
    """Constrói os 5 headers L2 para uma request autenticada.

    Args:
        address: wallet address do signer.
        api_key, secret, passphrase: credenciais L2 derivadas no startup.
        method: "POST" / "DELETE" / "GET" (uppercase).
        path: caminho da request (e.g. "/order"), sem host/query.
        body_json: corpo JSON serializado (separators=(",", ":") sem
                   espaços) ou None para GET sem body.

    Returns:
        dict com 5 chaves prontas para `httpx.AsyncClient.post(headers=...)`.
    """
    timestamp = int(time.time())
    signature = _hmac_signature(secret, timestamp, method, path, body_json)
    return {
        POLY_ADDRESS: address,
        POLY_SIGNATURE: signature,
        POLY_TIMESTAMP: str(timestamp),
        POLY_API_KEY: api_key,
        POLY_PASSPHRASE: passphrase,
    }
