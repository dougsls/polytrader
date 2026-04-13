"""Polygon USDC.e balance fetcher.

Lê o saldo on-chain da carteira via web3/Alchemy/public RPC. Consumido
pelo `BalanceCache` em intervalos curtos — não é chamado no hot path.

USDC.e (bridged) é o token que a Polymarket usa como colateral.
Contract: 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
6 decimais. O retorno é em USD humano (divide por 1e6).
"""
from __future__ import annotations

import asyncio

from src.core.logger import get_logger

log = get_logger(__name__)

USDC_E_POLYGON = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_DECIMALS = 6
DEFAULT_RPC = "https://polygon-rpc.com"

# ABI mínima para balanceOf(address)
ERC20_BALANCE_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    }
]


class USDCBalanceFetcher:
    """Callable que o BalanceCache chama periodicamente."""

    def __init__(self, wallet_address: str, rpc_url: str = DEFAULT_RPC) -> None:
        if not wallet_address:
            raise ValueError("wallet_address vazio")
        self._wallet = wallet_address
        self._rpc = rpc_url

    async def __call__(self) -> float:
        def _fetch() -> float:
            from web3 import Web3  # type: ignore[import-not-found]

            w3 = Web3(Web3.HTTPProvider(self._rpc, request_kwargs={"timeout": 10}))
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(USDC_E_POLYGON),
                abi=ERC20_BALANCE_ABI,
            )
            raw = contract.functions.balanceOf(
                Web3.to_checksum_address(self._wallet)
            ).call()
            return float(raw) / (10 ** USDC_DECIMALS)

        loop = asyncio.get_running_loop()
        value = await loop.run_in_executor(None, _fetch)
        return value
