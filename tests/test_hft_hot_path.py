"""HFT hot-path tests:
  - clob_l2_auth: HMAC determinístico, paridade com SDK reference
  - order_manager: build_draft puro RAM (zero disk I/O)
  - persist_trade_async: fire-and-forget INSERT idempotente
  - HeartbeatWatchdog: silence/zombie detection
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.clob_l2_auth import (
    POLY_ADDRESS,
    POLY_API_KEY,
    POLY_PASSPHRASE,
    POLY_SIGNATURE,
    POLY_TIMESTAMP,
    _hmac_signature,
    build_l2_headers,
)
from src.api.heartbeat import HeartbeatWatchdog


# ============ clob_l2_auth: HMAC determinístico ============

def test_hmac_matches_manual_construction():
    """Verifica paridade 1:1 com a fórmula da SDK oficial Polymarket."""
    secret_b64 = "YWJjZGVmZ2hpag=="  # "abcdefghij" em base64url
    timestamp = 1700000000
    method = "POST"
    path = "/order"
    body = '{"a":1}'

    expected = hmac.new(
        base64.urlsafe_b64decode(secret_b64),
        f"{timestamp}{method}{path}{body}".encode(),
        hashlib.sha256,
    ).digest()
    expected_b64 = base64.urlsafe_b64encode(expected).decode()

    assert _hmac_signature(secret_b64, timestamp, method, path, body) == expected_b64


def test_hmac_handles_empty_body():
    sig = _hmac_signature("YWJjZGVmZ2hpag==", 1700000000, "GET", "/positions", None)
    assert isinstance(sig, str) and len(sig) > 20


def test_hmac_replaces_single_quotes_for_go_ts_parity():
    """SDK oficial troca ' por " — replicamos pra HMAC bater com servidor."""
    body_with_singles = "{'a':1}"
    body_with_doubles = '{"a":1}'
    sig1 = _hmac_signature("YWJjZGVmZ2hpag==", 1700000000, "POST", "/x", body_with_singles)
    sig2 = _hmac_signature("YWJjZGVmZ2hpag==", 1700000000, "POST", "/x", body_with_doubles)
    assert sig1 == sig2


def test_build_l2_headers_returns_5_fields():
    headers = build_l2_headers(
        address="0xabc", api_key="kkk", secret="YWJjZGVmZ2hpag==", passphrase="ppp",
        method="POST", path="/order", body_json='{"x":1}',
    )
    assert headers[POLY_ADDRESS] == "0xabc"
    assert headers[POLY_API_KEY] == "kkk"
    assert headers[POLY_PASSPHRASE] == "ppp"
    assert int(headers[POLY_TIMESTAMP]) > 1_600_000_000  # >= 2020
    assert isinstance(headers[POLY_SIGNATURE], str)
    assert len(headers) == 5


# ============ order_manager: build_draft puro RAM ============

@pytest.mark.asyncio
async def test_build_draft_does_not_touch_disk(tmp_path: Path):
    """build_draft NÃO deve abrir conexão SQLite. Verifica via path inexistente."""
    from src.executor.order_manager import build_draft
    from src.core.models import TradeSignal

    # db_path inexistente — se build_draft tocar disco, vai falhar.
    fake_db = tmp_path / "DOES_NOT_EXIST.db"

    gamma = MagicMock()
    gamma.get_market = AsyncMock(return_value={
        "tick_size": 0.01, "neg_risk": False,
    })
    cfg = MagicMock()
    cfg.mode = "paper"
    cfg.paper_perfect_mirror = False
    cfg.limit_price_offset = 0.02
    cfg.paper_apply_fees = 0.0
    cfg.paper_simulate_slippage = 0.0

    signal = TradeSignal(
        id="sig-1", wallet_address="0xWHALE", wallet_score=0.8,
        condition_id="0xCID", token_id="tok-1", side="BUY",
        size=100.0, price=0.50, usd_value=50.0,
        market_title="X?", outcome="Yes",
        detected_at=datetime.now(timezone.utc), source="polling",
    )
    draft, trade, spec = await build_draft(
        signal=signal, sized_usd=10.0, ref_price=0.50,
        gamma=gamma, cfg=cfg, db_path=fake_db,
    )
    assert trade.id != ""
    assert trade.status == "pending"
    assert float(draft.price) > 0
    # Confirma que o arquivo não foi criado:
    assert not fake_db.exists()


async def _seed_signal(db: Path, signal_id: str, wallet: str = "0xWHALE") -> None:
    """Helper: cria trade_signals + tracked_wallets para FK do copy_trades."""
    from src.core.database import get_connection
    now = datetime.now(timezone.utc).isoformat()
    async with get_connection(db) as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO tracked_wallets"
            " (address, score, tracked_since, updated_at) VALUES (?,?,?,?)",
            (wallet, 0.8, now, now),
        )
        await conn.execute(
            "INSERT OR IGNORE INTO trade_signals"
            " (id, wallet_address, wallet_score, condition_id, token_id, side,"
            "  size, price, usd_value, market_title, outcome, detected_at, source)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (signal_id, wallet, 0.8, "0xC", "t", "BUY",
             10.0, 0.5, 5.0, "X?", "Yes", now, "polling"),
        )
        await conn.commit()


@pytest.mark.asyncio
async def test_persist_trade_async_inserts(tmp_path: Path):
    """persist_trade_async grava na tabela copy_trades."""
    from src.core.database import init_database, get_connection
    from src.core.models import CopyTrade
    from src.executor.order_manager import persist_trade_async

    db = tmp_path / "p.db"
    await init_database(db)
    await _seed_signal(db, "sig-1")

    trade = CopyTrade(
        id="tr-1", signal_id="sig-1", condition_id="0xC", token_id="t",
        side="BUY", intended_size=10.0, intended_price=0.5,
        status="pending", created_at=datetime.now(timezone.utc),
    )
    await persist_trade_async(trade, db_path=db)

    async with get_connection(db) as conn:
        async with conn.execute(
            "SELECT id, status FROM copy_trades WHERE id=?", ("tr-1",)
        ) as cur:
            row = await cur.fetchone()
    assert row is not None
    assert row[0] == "tr-1"
    assert row[1] == "pending"


@pytest.mark.asyncio
async def test_persist_trade_idempotent_does_not_overwrite_status(tmp_path: Path):
    """⚠️ Item 3 fix — INSERT OR IGNORE preserva status posterior.

    Fluxo agora:
      1. persist_trade(status='pending') antes do post_order
      2. update_trade_status(status='filled') depois do fill
      3. Se persist_trade rodar de novo (race/retry) NÃO deve sobrescrever
         o status='filled' com 'pending'.
    """
    from src.core.database import init_database, get_connection
    from src.core.models import CopyTrade
    from src.executor.order_manager import persist_trade_async, update_trade_status

    db = tmp_path / "p.db"
    await init_database(db)
    await _seed_signal(db, "sig-x")

    trade1 = CopyTrade(
        id="tr-x", signal_id="sig-x", condition_id="c", token_id="t",
        side="BUY", intended_size=10.0, intended_price=0.5,
        status="pending", created_at=datetime.now(timezone.utc),
    )
    await persist_trade_async(trade1, db_path=db)
    # Simula UPDATE de status posterior (post_order success → submitted →
    # fill confirmed → 'filled').
    await update_trade_status(trade1.id, status="filled", db_path=db)
    # Race: persist_trade roda de novo (idempotência defensiva).
    trade1.status = "pending"  # forçar pra simular call duplicado
    await persist_trade_async(trade1, db_path=db)

    async with get_connection(db) as conn:
        async with conn.execute(
            "SELECT status FROM copy_trades WHERE id=?", ("tr-x",)
        ) as cur:
            row = await cur.fetchone()
    # Status FINAL preservado — INSERT OR IGNORE não destrói UPDATE.
    assert row[0] == "filled"


# ============ HeartbeatWatchdog: silence detection ============

@pytest.mark.asyncio
async def test_silence_detection_triggers_on_failure():
    """Sem mensagens por > silence_threshold, on_failure dispara."""
    failure_called = asyncio.Event()

    async def send_ok() -> None:
        return None

    async def on_fail() -> None:
        failure_called.set()

    wd = HeartbeatWatchdog(
        send_heartbeat=send_ok, on_failure=on_fail,
        interval_seconds=10.0,                # ping irrelevante para esse teste
        silence_threshold_seconds=0.3,        # 300ms para teste rápido
    )
    wd.start()
    # Simula 1 mensagem inicial — acionará o silence loop.
    wd.notify_message_received()
    # Sem novas notify, espera o timeout (300ms + 500ms check = 800ms max).
    await asyncio.wait_for(failure_called.wait(), timeout=2.0)
    await wd.stop()
    assert failure_called.is_set()


@pytest.mark.asyncio
async def test_silence_detection_resets_on_message():
    """Mensagens contínuas devem manter o watchdog quieto."""
    failure_called = asyncio.Event()

    async def send_ok() -> None:
        return None

    async def on_fail() -> None:
        failure_called.set()

    wd = HeartbeatWatchdog(
        send_heartbeat=send_ok, on_failure=on_fail,
        interval_seconds=10.0,
        silence_threshold_seconds=0.5,
    )
    wd.start()
    # Envia "mensagens" continuamente por 1s — silêncio nunca passa de 100ms.
    for _ in range(20):
        wd.notify_message_received()
        await asyncio.sleep(0.05)
    await wd.stop()
    assert not failure_called.is_set()


@pytest.mark.asyncio
async def test_silence_detection_disabled_when_threshold_none():
    """silence_threshold=None desliga totalmente (default seguro)."""
    failure_called = asyncio.Event()

    async def send_ok() -> None:
        return None

    async def on_fail() -> None:
        failure_called.set()

    wd = HeartbeatWatchdog(
        send_heartbeat=send_ok, on_failure=on_fail,
        interval_seconds=10.0,
        silence_threshold_seconds=None,
    )
    wd.start()
    await asyncio.sleep(0.5)
    await wd.stop()
    assert not failure_called.is_set()
    # Confirma que o silence_task nem foi criado.
    assert wd._silence_task is None
