"""Track A — Pure-arbitrage engine para Polymarket.

Independente do copy-trader. Edge matemático: YES + NO complementares
de uma mesma condition somam $1 ao redimir. Se ask_yes + ask_no < 1,
comprar ambos no CLOB e dar mergePositions no CTF garante $1 USDC ─
lucro = (1 - ask_yes - ask_no - fees) * size.

Módulos:
  - models      → ArbOpportunity, ArbExecution Pydantic
  - scanner     → varre Gamma+CLOB continuamente, detecta YES+NO<1
  - ctf_client  → web3 wrapper para mergePositions/redeemPositions
  - executor    → multi-leg atomic com rollback
"""
from __future__ import annotations
