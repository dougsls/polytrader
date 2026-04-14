"""On-chain approvals para Polymarket — UMA execução, ~$0.05 em gas.

Aprova:
  1. USDC.approve(CTFExchange, MAX_UINT256)
  2. ConditionalTokens.setApprovalForAll(CTFExchange, true)
  3. ConditionalTokens.setApprovalForAll(NegRiskCTFExchange, true)
  4. ConditionalTokens.setApprovalForAll(NegRiskAdapter, true)

Sem essas aprovações, primeira ordem real cai com:
"insufficient allowance" no CLOB.

Uso:
    cd /opt/polytrader && .venv/bin/python scripts/approve_polymarket.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eth_account import Account
from web3 import Web3

from src.core.config import get_settings


# === Polymarket Polygon mainnet addresses ===
USDC_E = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
CTF = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
CTF_EXCHANGE = Web3.to_checksum_address("0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E")
NEG_RISK_EXCHANGE = Web3.to_checksum_address("0xC5d563A36AE78145C45a50134d48A1215220f80a")
NEG_RISK_ADAPTER = Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")

MAX_UINT256 = (1 << 256) - 1

ERC20_ABI = [
    {"constant": False, "inputs": [
        {"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "name": "approve", "outputs": [{"name": "", "type": "bool"}],
     "type": "function"},
    {"constant": True, "inputs": [
        {"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "name": "allowance", "outputs": [{"name": "", "type": "uint256"}],
     "type": "function"},
    {"constant": True, "inputs": [{"name": "owner", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
     "type": "function"},
]

CTF_ABI = [
    {"inputs": [
        {"name": "operator", "type": "address"}, {"name": "approved", "type": "bool"}],
     "name": "setApprovalForAll", "outputs": [], "type": "function"},
    {"inputs": [
        {"name": "owner", "type": "address"}, {"name": "operator", "type": "address"}],
     "name": "isApprovedForAll", "outputs": [{"name": "", "type": "bool"}],
     "type": "function"},
]

# Polygon RPCs (fallback chain — public are flaky)
RPCS = [
    "https://polygon-rpc.com",
    "https://rpc-mainnet.matic.network",
    "https://polygon.llamarpc.com",
    "https://polygon.drpc.org",
]


def get_w3() -> Web3:
    for url in RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 10}))
            if w3.is_connected():
                print(f"  → conectado em {url}")
                return w3
        except Exception:
            continue
    raise RuntimeError("Nenhum RPC Polygon respondeu")


def main() -> int:
    s = get_settings().env
    if not s.private_key:
        print("❌ PRIVATE_KEY vazio em .env"); return 1

    print("→ Conectando à Polygon mainnet...")
    w3 = get_w3()
    acct = Account.from_key(s.private_key)
    addr = acct.address
    funder = Web3.to_checksum_address(s.funder_address) if s.funder_address else addr

    print(f"  EOA address: {addr}")
    print(f"  Funder:      {funder}")
    if addr.lower() != funder.lower():
        print(f"  ⚠️  EOA != funder. Approvals devem ser feitas pela funder address.")
        print(f"     Se funder é um Safe/Proxy, use a interface dele.")

    matic_bal = w3.eth.get_balance(addr) / 1e18
    print(f"  MATIC balance: {matic_bal:.4f}")
    if matic_bal < 0.1:
        print("❌ Saldo MATIC muito baixo. Deposite ~0.5 MATIC pra cobrir gas (~$0.30).")
        return 2

    usdc = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)
    usdc_bal = usdc.functions.balanceOf(addr).call() / 1e6
    print(f"  USDC balance: ${usdc_bal:.2f}")

    # Targets a aprovar
    targets = [
        ("USDC → CTF Exchange",       "erc20",  CTF_EXCHANGE),
        ("USDC → NegRisk Exchange",   "erc20",  NEG_RISK_EXCHANGE),
        ("USDC → NegRisk Adapter",    "erc20",  NEG_RISK_ADAPTER),
        ("CTF → CTF Exchange",        "ctf",    CTF_EXCHANGE),
        ("CTF → NegRisk Exchange",    "ctf",    NEG_RISK_EXCHANGE),
        ("CTF → NegRisk Adapter",     "ctf",    NEG_RISK_ADAPTER),
    ]

    ctf_contract = w3.eth.contract(address=CTF, abi=CTF_ABI)
    nonce = w3.eth.get_transaction_count(addr)
    print(f"\n→ Verificando approvals existentes...")
    pending = []

    for name, kind, spender in targets:
        if kind == "erc20":
            allow = usdc.functions.allowance(addr, spender).call()
            ok = allow > 10 ** 30  # near-max
            print(f"  {'✅' if ok else '❌'} {name}: allowance={allow / 1e6:.0f}")
            if not ok:
                pending.append((name, kind, spender))
        else:
            ok = ctf_contract.functions.isApprovedForAll(addr, spender).call()
            print(f"  {'✅' if ok else '❌'} {name}: approvedForAll={ok}")
            if not ok:
                pending.append((name, kind, spender))

    if not pending:
        print("\n✅ Todas as approvals já feitas. Nada a fazer.")
        return 0

    print(f"\n→ Faltam {len(pending)} approvals. Custo estimado: ~${len(pending) * 0.05:.2f} em gas.")
    confirm = input("Continuar? (s/N) ").strip().lower()
    if confirm != "s":
        print("Abortado."); return 0

    chain_id = 137  # Polygon mainnet
    base_fee = w3.eth.gas_price
    # Margem de 20% pro priority fee
    max_fee = int(base_fee * 1.5)
    priority = int(base_fee * 0.2)

    for name, kind, spender in pending:
        print(f"\n→ Enviando: {name}")
        if kind == "erc20":
            fn = usdc.functions.approve(spender, MAX_UINT256)
        else:
            fn = ctf_contract.functions.setApprovalForAll(spender, True)
        tx = fn.build_transaction({
            "from": addr,
            "nonce": nonce,
            "chainId": chain_id,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority,
            "gas": 100_000,
        })
        signed = acct.sign_transaction(tx)
        h = w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f"  tx hash: {h.hex()}")
        rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=120)
        if rcpt.status != 1:
            print(f"  ❌ FALHOU. Receipt: {rcpt}"); return 3
        print(f"  ✅ confirmado · gas usado: {rcpt.gasUsed}")
        nonce += 1

    print("\n🎉 Todas as approvals concluídas. Pronto pra trade live.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
