"""One-time wallet setup: deriva credenciais L2 e imprime diagnóstico.

Executar uma vez após configurar `.env` com PRIVATE_KEY + FUNDER_ADDRESS.

Uso: uv run python scripts/setup_wallet.py

Checks:
  1. Credenciais L2 derivadas com sucesso (API key/secret/passphrase)
  2. Saldo USDC.e atual na carteira funder
  3. (opcional) allowances para o CTF exchange — log informativo

Importante: NÃO grava chaves em disco. L2 creds vivem só em RAM do bot.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api.auth import prefetch_credentials
from src.api.balance import USDCBalanceFetcher
from src.core.config import get_settings


async def main() -> int:
    s = get_settings().env

    if not s.private_key:
        print("❌ PRIVATE_KEY vazio em .env. Edite antes de rodar.")
        return 1
    if not s.funder_address:
        print("⚠️  FUNDER_ADDRESS vazio. Para EOA standalone, iguale ao address da PRIVATE_KEY.")

    print("→ Derivando credenciais L2…")
    try:
        signed = await prefetch_credentials(
            private_key=s.private_key,
            funder=s.funder_address,
            signature_type=s.signature_type,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Falha ao derivar: {exc!r}")
        return 2

    print(f"✅ L2 ok — address={signed.credentials.address[:12]}… "
          f"api_key={signed.credentials.api_key[:8]}…")

    if s.funder_address:
        print("→ Consultando saldo USDC.e on-chain…")
        try:
            bal = await USDCBalanceFetcher(s.funder_address)()
            print(f"✅ Saldo USDC.e: ${bal:,.2f}")
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️  Falha ao consultar saldo (segue mesmo assim): {exc!r}")

    print("\nWallet pronta. Rode `sudo systemctl start polytrader` na VPS.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
