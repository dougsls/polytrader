"""24h DEMO REALISTA — simula execução com livro REAL do CLOB.

⚠️ Diferente do paper_perfect_mirror (fill mágico no preço da whale),
este simulador é CONSERVADOR e fiel à realidade:

  1. Lê o book real no momento do sinal (não usa o whale_price como
     liquidação garantida).
  2. Caminha o livro consumindo liquidez nível-a-nível (VWAP).
  3. Decide:
        a) Se book vazio em algum lado → SKIPPED com motivo DEPTH_EMPTY.
        b) Se spread atual > cfg.max_spread → SKIPPED com SPREAD_WIDE.
        c) Se preço efetivo (VWAP) viola whale_max_slippage_pct vs
           whale_price → SKIPPED com SLIPPAGE_HIGH.
        d) Se depth disponível < intended × min_fill_pct →
           PARTIAL_SIMULATED com simulated_size = depth disponível.
        e) Caso contrário → EXECUTED com simulated_price = VWAP.
  4. Stale/emergency exits (`bypass_slippage_check=True`) IGNORAM (b)
     e (c) mas ainda persistem no journal com `is_emergency_exit=1`.
     Book vazio (a) ainda bloqueia — sem onde vender.

Saída: `SimulationResult` com toda a auditoria pra persistir no
`demo_journal`. Caller (copy_engine) decide se aplica fill local
(executed/partial) ou marca skipped.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from src.api.clob_client import CLOBClient
from src.core.config import ExecutorConfig
from src.core.logger import get_logger
from src.core.models import TradeSignal
from src.executor.depth_sizing import quote_buy_depth, quote_sell_depth

log = get_logger(__name__)

SimStatus = Literal["executed", "partial", "skipped"]
# Fração mínima de fill pra contar como "partial" (vs skipped).
# 30% = se depth só permite ≤30% do intended, marca skipped.
DEFAULT_MIN_FILL_PCT = 0.30


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """Resultado da simulação realista — auditável.

    O caller (copy_engine) usa `status` pra decidir aplicar fill ou
    skip; persiste TODO o resto em `demo_journal`.
    """
    status: SimStatus
    skip_reason: str | None
    # Preços
    whale_price: float
    ref_price: float                # best ask (BUY) / best bid (SELL)
    simulated_price: float          # VWAP do consumo
    spread: float
    slippage: float                 # (simulated - whale) / whale
    # Sizing
    intended_size: float            # tokens
    simulated_size: float           # tokens efetivamente "filled"
    depth_usd: float                # depth observado no consumo
    levels_consumed: int
    book_thin: bool
    is_emergency_exit: bool

    @property
    def filled(self) -> bool:
        return self.status in ("executed", "partial")


def _best_ask(book: dict[str, Any]) -> float:
    asks = book.get("asks") or []
    if not asks:
        return 0.0
    return min(float(a["price"]) for a in asks)


def _best_bid(book: dict[str, Any]) -> float:
    bids = book.get("bids") or []
    if not bids:
        return 0.0
    return max(float(b["price"]) for b in bids)


def _spread(book: dict[str, Any]) -> float:
    bid = _best_bid(book)
    ask = _best_ask(book)
    if ask <= 0 or bid <= 0:
        return float("inf")
    return ask - bid


def simulate_fill(
    *,
    signal: TradeSignal,
    book: dict[str, Any] | None,
    intended_size: float,
    intended_price: float,
    cfg: ExecutorConfig,
    depth_max_levels: int = 5,
    min_fill_pct: float = DEFAULT_MIN_FILL_PCT,
) -> SimulationResult:
    """Simula execução conservadora.

    Args:
        signal: TradeSignal do tracker (whale_price, side, etc.).
        book: Polymarket book {"asks": [...], "bids": [...]} ou None.
              None = falha de book fetch → SKIPPED com BOOK_FETCH_FAIL.
        intended_size: tokens que o bot pretendia tradear (pós-quantize).
        intended_price: preço limite quantizado.
        cfg: ExecutorConfig (whale_max_slippage_pct, max_spread).
        depth_max_levels: limite de níveis pro VWAP walk.
        min_fill_pct: fração mínima pra marcar partial vs skipped.
    """
    is_emergency = bool(getattr(signal, "bypass_slippage_check", False))
    whale_price = float(signal.price)

    # --- 1. Book fetch fail / vazio ---
    if book is None:
        return SimulationResult(
            status="skipped", skip_reason="BOOK_FETCH_FAIL",
            whale_price=whale_price, ref_price=0.0, simulated_price=0.0,
            spread=float("inf"), slippage=0.0,
            intended_size=intended_size, simulated_size=0.0,
            depth_usd=0.0, levels_consumed=0, book_thin=True,
            is_emergency_exit=is_emergency,
        )

    side = signal.side
    if side == "BUY":
        ref = _best_ask(book)
        if ref <= 0:
            return SimulationResult(
                status="skipped", skip_reason="DEPTH_EMPTY",
                whale_price=whale_price, ref_price=0.0, simulated_price=0.0,
                spread=float("inf"), slippage=0.0,
                intended_size=intended_size, simulated_size=0.0,
                depth_usd=0.0, levels_consumed=0, book_thin=True,
                is_emergency_exit=is_emergency,
            )
    else:
        ref = _best_bid(book)
        if ref <= 0:
            return SimulationResult(
                status="skipped", skip_reason="DEPTH_EMPTY",
                whale_price=whale_price, ref_price=0.0, simulated_price=0.0,
                spread=float("inf"), slippage=0.0,
                intended_size=intended_size, simulated_size=0.0,
                depth_usd=0.0, levels_consumed=0, book_thin=True,
                is_emergency_exit=is_emergency,
            )

    spread = _spread(book)

    # --- 2. Spread shield (BUY+SELL, exceto emergency) ---
    if not is_emergency and spread > cfg.max_spread:
        return SimulationResult(
            status="skipped", skip_reason="SPREAD_WIDE",
            whale_price=whale_price, ref_price=ref, simulated_price=0.0,
            spread=spread, slippage=0.0,
            intended_size=intended_size, simulated_size=0.0,
            depth_usd=0.0, levels_consumed=0,
            book_thin=False, is_emergency_exit=is_emergency,
        )

    # --- 3. Walk do VWAP ---
    target_usd = intended_size * intended_price
    if target_usd <= 0:
        return SimulationResult(
            status="skipped", skip_reason="INVALID_SIZE",
            whale_price=whale_price, ref_price=ref, simulated_price=0.0,
            spread=spread, slippage=0.0,
            intended_size=intended_size, simulated_size=0.0,
            depth_usd=0.0, levels_consumed=0,
            book_thin=False, is_emergency_exit=is_emergency,
        )
    # Em emergency exit: relaxa max_impact pra book_thin não barrar dump
    max_impact = 1.0 if is_emergency else cfg.whale_max_slippage_pct
    if side == "BUY":
        q = quote_buy_depth(book, target_usd, max_impact, depth_max_levels)
    else:
        q = quote_sell_depth(book, target_usd, max_impact, depth_max_levels)

    if q.fillable_size_usd <= 0:
        return SimulationResult(
            status="skipped", skip_reason="DEPTH_EMPTY",
            whale_price=whale_price, ref_price=ref, simulated_price=0.0,
            spread=spread, slippage=0.0,
            intended_size=intended_size, simulated_size=0.0,
            depth_usd=0.0, levels_consumed=0, book_thin=q.book_thin,
            is_emergency_exit=is_emergency,
        )

    # Slippage vs whale_price
    if whale_price > 0:
        if side == "BUY":
            slippage = (q.vwap_price - whale_price) / whale_price
        else:
            slippage = (whale_price - q.vwap_price) / whale_price
    else:
        slippage = 0.0

    # --- 4. Slippage cap (BUY+SELL, exceto emergency) ---
    if not is_emergency and slippage > cfg.whale_max_slippage_pct:
        return SimulationResult(
            status="skipped", skip_reason="SLIPPAGE_HIGH",
            whale_price=whale_price, ref_price=ref,
            simulated_price=q.vwap_price, spread=spread, slippage=slippage,
            intended_size=intended_size, simulated_size=0.0,
            depth_usd=q.fillable_size_usd, levels_consumed=q.levels_consumed,
            book_thin=q.book_thin, is_emergency_exit=is_emergency,
        )

    # --- 5. Sizing efetivo (parcial?) ---
    sim_tokens = q.fillable_size_usd / max(q.vwap_price, 0.01)
    fill_pct = sim_tokens / intended_size if intended_size > 0 else 0.0
    if fill_pct < min_fill_pct and not is_emergency:
        return SimulationResult(
            status="skipped", skip_reason="DEPTH_TOO_THIN",
            whale_price=whale_price, ref_price=ref,
            simulated_price=q.vwap_price, spread=spread, slippage=slippage,
            intended_size=intended_size, simulated_size=sim_tokens,
            depth_usd=q.fillable_size_usd, levels_consumed=q.levels_consumed,
            book_thin=q.book_thin, is_emergency_exit=is_emergency,
        )

    # --- 6. Fill efetivo: total ou parcial ---
    if sim_tokens >= intended_size - 1e-9:
        status: SimStatus = "executed"
        sim_tokens = intended_size  # cap em intended (sem over-fill)
    else:
        status = "partial"

    return SimulationResult(
        status=status, skip_reason=None,
        whale_price=whale_price, ref_price=ref,
        simulated_price=q.vwap_price, spread=spread, slippage=slippage,
        intended_size=intended_size, simulated_size=sim_tokens,
        depth_usd=q.fillable_size_usd, levels_consumed=q.levels_consumed,
        book_thin=q.book_thin, is_emergency_exit=is_emergency,
    )


async def fetch_book_safe(clob: CLOBClient, token_id: str) -> dict[str, Any] | None:
    """Wrapper que retorna None em vez de propagar — caller usa pra
    construir SimulationResult com BOOK_FETCH_FAIL."""
    try:
        return await clob.book(token_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("simulator_book_fetch_failed",
                    token_id=token_id[:12], err=repr(exc)[:80])
        return None
