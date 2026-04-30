"""ConditionalTokens (CTF) wrapper — merge/redeem on Polygon mainnet.

A operação chave para arbitragem YES+NO < 1:

  Após comprar size N de YES e size N de NO no CLOB, chamamos
  CTF.mergePositions(USDC, 0x0, conditionId, [1,2], N). Isso queima N
  unidades de cada token e devolve N USDC pro operador.

  Lucro = N * (1 - vwap_yes - vwap_no - fees)

Para neg-risk markets (multi-outcome) o merge passa pelo NegRiskAdapter
com partition incluindo TODOS os outcome indices. Fase 2.

QUANT/MEV — refactor 2026-04:
    - **AsyncWeb3 nativo**: zero `run_in_executor`. Toda chamada RPC é
      `await w3.eth.*` direto no event loop. `wait_for_transaction_receipt`
      em AsyncWeb3 usa `asyncio.sleep` internamente — não prende thread
      do default ThreadPoolExecutor por até 60s como fazia antes.
    - **EIP-1559 dinâmico**: `maxPriorityFeePerGas` vem do `GasOracle`
      (Polygon Gas Station v2 → eth_feeHistory p99 → hardcoded 30 gwei).
      Em volatilidade, isso evita tx ficar fora do bloco / MEV-sandwich.
"""
from __future__ import annotations

import asyncio
from typing import Any

from src.arbitrage.gas_oracle import GasOracle
from src.core.logger import get_logger

log = get_logger(__name__)

# ABIs minimal — só os métodos que chamamos. Evita carregar o ABI completo.
_CTF_ABI: list[dict[str, Any]] = [
    {
        "name": "mergePositions",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "partition", "type": "uint256[]"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "name": "redeemPositions",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "outputs": [],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "isApprovedForAll",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
]

# Decimals — Polymarket CTF posições são denominadas em 6 (igual USDC).
_TOKEN_DECIMALS = 6
# Polygon block time ~2.2s; 60s = ~25 blocks de margem.
_TX_RECEIPT_TIMEOUT_S = 60
# Polling interval do receipt — 1s = ~0.5 bloco. Sub-bloco causa spam RPC.
_TX_RECEIPT_POLL_S = 1.0


class CTFClient:
    """Wrapper async em torno do contrato ConditionalTokens.

    Uso típico:
        ctf = CTFClient(rpc_url, ctf_address, usdc_address, private_key)
        tx_hash = await ctf.merge_binary(condition_id, size=10.0)
    """

    def __init__(
        self,
        rpc_url: str,
        ctf_address: str,
        usdc_address: str,
        private_key: str | None,
        funder_address: str | None,
        gas_oracle: GasOracle | None = None,
    ) -> None:
        self._rpc_url = rpc_url
        self._ctf_address = ctf_address
        self._usdc_address = usdc_address
        self._private_key = private_key
        self._funder = funder_address
        self._w3: Any = None
        self._contract: Any = None
        self._account_address: str | None = None
        self._init_lock = asyncio.Lock()
        # Gas oracle injetável — facilita testes e permite share do cache
        # entre múltiplos CTFClients (caso suportado no futuro).
        self._gas_oracle = gas_oracle if gas_oracle is not None else GasOracle()

    async def _ensure_initialized(self) -> None:
        """Lazy init thread-safe via asyncio.Lock — protege contra
        múltiplas corotinas chamando merge_binary concorrentemente no
        primeiro uso. Após inicializar, lock é no-op (early return)."""
        if self._w3 is not None:
            return
        async with self._init_lock:
            if self._w3 is not None:
                return
            from web3 import AsyncWeb3, AsyncHTTPProvider  # type: ignore[import-not-found]
            from web3.middleware import ExtraDataToPOAMiddleware  # type: ignore[import-not-found]

            w3 = AsyncWeb3(AsyncHTTPProvider(
                self._rpc_url, request_kwargs={"timeout": 30},
            ))
            # Polygon usa POA — middleware preserva extraData além do
            # limite default do Geth. Funciona em sync e async em web3 7.x.
            try:
                w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            except Exception:  # noqa: BLE001 — older web3 differs
                pass
            connected = await w3.is_connected()
            if not connected:
                raise RuntimeError(f"web3 não conectou em {self._rpc_url}")
            self._w3 = w3
            self._contract = w3.eth.contract(
                address=AsyncWeb3.to_checksum_address(self._ctf_address),
                abi=_CTF_ABI,
            )
            if self._private_key:
                from eth_account import Account  # type: ignore[import-not-found]

                acct = Account.from_key(self._private_key)
                self._account_address = acct.address
            elif self._funder:
                self._account_address = self._funder

    @staticmethod
    def _to_bytes32(hex_str: str) -> bytes:
        s = hex_str.lower().removeprefix("0x")
        if len(s) != 64:
            raise ValueError(f"conditionId inválido: {hex_str} (esperado 32 bytes hex)")
        return bytes.fromhex(s)

    @staticmethod
    def _to_units(amount: float) -> int:
        """Converte tokens (decimal humano) para unidades on-chain (6 decimals)."""
        return int(round(amount * (10 ** _TOKEN_DECIMALS)))

    async def _build_eip1559_fees(self) -> tuple[int, int]:
        """Retorna (max_fee_wei, max_priority_wei) usando gas oracle dinâmico.

        Estratégia conservadora pra inclusão garantida:
            maxFeePerGas = base_fee × 2 + max_priority_fee
        2× baseFee como buffer contra o spike entre estimação e inclusão
        no próximo bloco (Polygon move rápido).
        """
        max_priority_wei = await self._gas_oracle.get_priority_fee_wei(self._w3)
        try:
            block = await self._w3.eth.get_block("latest")
            base_fee = block["baseFeePerGas"]
        except (KeyError, Exception):  # noqa: BLE001 — sem EIP-1559 no node
            # Fallback raríssimo em RPCs antigos: usa só priority como max.
            return max_priority_wei * 2, max_priority_wei
        max_fee_wei = base_fee * 2 + max_priority_wei
        return max_fee_wei, max_priority_wei

    async def _send_tx_and_wait(self, fn: Any) -> tuple[str, Any]:
        """Pipeline comum de submissão: estimate gas → build tx → sign →
        send → wait receipt. Compartilhado entre merge e redeem.

        Retorna (tx_hash_hex, receipt). Levanta RuntimeError se receipt
        veio com status=0 (revert on-chain).
        """
        from web3 import AsyncWeb3  # type: ignore[import-not-found]

        assert self._w3 is not None
        assert self._account_address is not None
        assert self._private_key is not None

        owner = AsyncWeb3.to_checksum_address(self._account_address)

        # Estimate gas. CTF mergePositions tipicamente ~150k; cap 500k.
        try:
            est = await fn.estimate_gas({"from": owner})
            gas = min(int(est * 1.2), 500_000)
        except Exception as exc:  # noqa: BLE001
            log.warning("ctf_gas_estimate_failed", err=repr(exc))
            gas = 350_000

        nonce = await self._w3.eth.get_transaction_count(owner, "pending")
        max_fee_wei, max_priority_wei = await self._build_eip1559_fees()

        try:
            tx = await fn.build_transaction({
                "from": owner,
                "nonce": nonce,
                "gas": gas,
                "maxFeePerGas": max_fee_wei,
                "maxPriorityFeePerGas": max_priority_wei,
                "chainId": 137,
            })
        except Exception as exc:  # noqa: BLE001 — fallback legacy gasPrice
            log.warning("eip1559_build_failed_fallback_legacy", err=repr(exc))
            gas_price = await self._w3.eth.gas_price
            tx = await fn.build_transaction({
                "from": owner, "nonce": nonce, "gas": gas,
                "gasPrice": gas_price, "chainId": 137,
            })

        # Signing é CPU-bound (~1ms keccak+ECDSA) — inline no loop é OK.
        signed = self._w3.eth.account.sign_transaction(tx, self._private_key)
        raw = (
            getattr(signed, "raw_transaction", None)
            or getattr(signed, "rawTransaction", None)
        )

        tx_hash = await self._w3.eth.send_raw_transaction(raw)
        tx_hash_hex = tx_hash.hex() if isinstance(tx_hash, (bytes, bytearray)) else str(tx_hash)
        log.info(
            "ctf_tx_submitted",
            tx=tx_hash_hex, gas=gas,
            max_priority_gwei=max_priority_wei / 1e9,
            max_fee_gwei=max_fee_wei / 1e9,
        )

        # AsyncWeb3.wait_for_transaction_receipt usa asyncio.sleep
        # internamente — NÃO bloqueia thread alguma do pool.
        receipt = await self._w3.eth.wait_for_transaction_receipt(
            tx_hash, timeout=_TX_RECEIPT_TIMEOUT_S, poll_latency=_TX_RECEIPT_POLL_S,
        )
        if receipt.status != 1:
            raise RuntimeError(f"CTF tx failed on-chain: {tx_hash_hex}")
        return tx_hash_hex, receipt

    async def merge_binary(self, condition_id: str, size: float) -> str:
        """mergePositions para mercado binário ([1,2]). Retorna tx_hash hex.

        Pré-condição: wallet detém >= size de YES e >= size de NO do
        condition_id. Caller é responsável por garantir.

        size = quantidade de pares YES+NO a queimar (= USDC retornado).
        """
        if not self._private_key:
            raise RuntimeError("CTF merge requer PRIVATE_KEY no .env")
        if size <= 0:
            raise ValueError("size deve ser > 0")
        await self._ensure_initialized()
        from web3 import AsyncWeb3  # type: ignore[import-not-found]

        amount_units = self._to_units(size)
        partition = [1, 2]
        parent = b"\x00" * 32
        cond = self._to_bytes32(condition_id)
        usdc = AsyncWeb3.to_checksum_address(self._usdc_address)

        fn = self._contract.functions.mergePositions(
            usdc, parent, cond, partition, amount_units,
        )
        tx_hash, receipt = await self._send_tx_and_wait(fn)
        log.info(
            "ctf_merge_confirmed",
            tx=tx_hash, cond_id=condition_id, size=size,
            gas_used=receipt.gasUsed,
        )
        return tx_hash

    async def redeem(self, condition_id: str, index_sets: list[int]) -> str:
        """redeemPositions — usado quando o mercado já resolveu.

        Em arb pura (merge antes do resolve) raramente é necessário, mas
        útil como fallback se uma op ficar pendurada e o mercado resolver.
        """
        if not self._private_key:
            raise RuntimeError("CTF redeem requer PRIVATE_KEY no .env")
        await self._ensure_initialized()
        from web3 import AsyncWeb3  # type: ignore[import-not-found]

        parent = b"\x00" * 32
        cond = self._to_bytes32(condition_id)
        usdc = AsyncWeb3.to_checksum_address(self._usdc_address)

        fn = self._contract.functions.redeemPositions(usdc, parent, cond, index_sets)
        tx_hash, _ = await self._send_tx_and_wait(fn)
        return tx_hash

    async def position_balance(self, token_id: str) -> float:
        """balanceOf do CTF para um token (positionId). Retorna float em
        unidades humanas (6 decimals). Útil pra confirmar fills antes de mergear.
        """
        await self._ensure_initialized()
        from web3 import AsyncWeb3  # type: ignore[import-not-found]

        assert self._contract is not None
        assert self._account_address is not None
        owner = AsyncWeb3.to_checksum_address(self._account_address)
        # token_id pode vir como int decimal-string OU hex; CTF positionId é uint256.
        tid = int(token_id, 0) if isinstance(token_id, str) else int(token_id)
        raw = await self._contract.functions.balanceOf(owner, tid).call()
        return float(raw) / (10 ** _TOKEN_DECIMALS)
