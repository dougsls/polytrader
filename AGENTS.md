# AGENTS.md

This file gives coding agents the minimum repo context needed to work quickly and safely.

## What This Repo Is

`polyclone` is a single-process async Polymarket copy-trading daemon.

Core runtime mental model:

1. `scanner` chooses which wallets to follow.
2. `tracker` turns wallet activity into `TradeSignal`s.
3. `executor` decides whether and how to copy them.
4. SQLite is the system of record.
5. `InMemoryState` is the hot-path mirror used to keep SELL mirroring safe.

The code is Python 3.12+, fully async, SQLite-backed, and started from [main.py](/Users/erik/Work/polyclone/main.py).

## Current Reality You Should Assume

- The scanner no longer depends on Polymarket `/leaderboard`.
- It starts from a static whale list plus optional manual overrides.
- Those wallets are enriched with live metrics, ranked, persisted into `tracked_wallets`, and applied to the active wallet set in place.
- Startup installs `uvloop` when available, loads config from `.env` + `config.yaml`, runs SQLite migrations, enables WAL, opens one shared `aiosqlite` connection, runs startup checks, reloads `InMemoryState`, then starts long-lived tasks.
- In live mode only, startup also reconciles on-chain bot positions into local state and prefetches CLOB auth.
- Default config is `paper`.
- Default config also sets `paper_perfect_mirror=true`, which bypasses many live-path protections. Do not trust paper behavior as proof of live correctness.

Important: parts of `README.md` still describe leaderboard discovery. The code path to trust is the implementation, not the older README narrative.

## Critical Invariants

### 1. SELL handling is strict inventory mirroring

SELLs should only be emitted and executed when both are true:

- the bot currently owns the token
- there is prior whale inventory state for that wallet/token

SELL size should mirror the fraction of whale inventory sold, not the whale's raw notional.

### 2. SQLite is source of truth

`InMemoryState` is a performance cache, not the canonical ledger.

If you change state flow, preserve this rule:

- DB writes define truth
- RAM mirrors committed truth
- reconciliation must converge RAM to DB/exchange truth

### 3. Live fills are more important than draft orders

If you touch execution, optimize for actual exchange truth:

- submitted
- partial fill
- filled
- cancelled
- rejected

Do not design around "post accepted == filled".

### 4. Fail closed on ambiguity

For live trading, missing metadata, stale balance, stale whale inventory, fill ambiguity, or reconciliation drift should bias toward skip/halt, not optimistic continuation.

## Most Important Files

Entry point and orchestration:

- [main.py](/Users/erik/Work/polyclone/main.py)

Scanner:

- [src/scanner/scanner.py](/Users/erik/Work/polyclone/src/scanner/scanner.py)
- [src/scanner/wallet_pool.py](/Users/erik/Work/polyclone/src/scanner/wallet_pool.py)
- [src/scanner/enrich.py](/Users/erik/Work/polyclone/src/scanner/enrich.py)
- [src/scanner/static_whales.py](/Users/erik/Work/polyclone/src/scanner/static_whales.py)

Tracker:

- [src/tracker/trade_monitor.py](/Users/erik/Work/polyclone/src/tracker/trade_monitor.py)
- [src/tracker/signal_detector.py](/Users/erik/Work/polyclone/src/tracker/signal_detector.py)
- [src/tracker/whale_inventory.py](/Users/erik/Work/polyclone/src/tracker/whale_inventory.py)
- [src/api/websocket_client.py](/Users/erik/Work/polyclone/src/api/websocket_client.py)

Executor:

- [src/executor/copy_engine.py](/Users/erik/Work/polyclone/src/executor/copy_engine.py)
- [src/executor/position_manager.py](/Users/erik/Work/polyclone/src/executor/position_manager.py)
- [src/executor/order_manager.py](/Users/erik/Work/polyclone/src/executor/order_manager.py)
- [src/executor/risk_manager.py](/Users/erik/Work/polyclone/src/executor/risk_manager.py)
- [src/executor/risk_state.py](/Users/erik/Work/polyclone/src/executor/risk_state.py)
- [src/executor/position_sync.py](/Users/erik/Work/polyclone/src/executor/position_sync.py)
- [src/executor/stale_cleanup.py](/Users/erik/Work/polyclone/src/executor/stale_cleanup.py)
- [src/executor/resolution_watcher.py](/Users/erik/Work/polyclone/src/executor/resolution_watcher.py)

API and metadata:

- [src/api/clob_client.py](/Users/erik/Work/polyclone/src/api/clob_client.py)
- [src/api/gamma_client.py](/Users/erik/Work/polyclone/src/api/gamma_client.py)
- [src/api/data_client.py](/Users/erik/Work/polyclone/src/api/data_client.py)
- [src/api/user_ws_client.py](/Users/erik/Work/polyclone/src/api/user_ws_client.py)

Core state and schema:

- [src/core/state.py](/Users/erik/Work/polyclone/src/core/state.py)
- [src/core/database.py](/Users/erik/Work/polyclone/src/core/database.py)
- [src/core/models.py](/Users/erik/Work/polyclone/src/core/models.py)
- [src/core/config.py](/Users/erik/Work/polyclone/src/core/config.py)
- [migrations/001_initial.sql](/Users/erik/Work/polyclone/migrations/001_initial.sql)

Tests:

- [tests/](/Users/erik/Work/polyclone/tests)

## Key Tables

- `tracked_wallets`: active and historical wallet pool
- `wallet_scores_history`: scanner scoring history
- `trade_signals`: tracker output and signal audit trail
- `copy_trades`: execution lifecycle records
- `bot_positions`: bot inventory and PnL state
- `whale_inventory`: prior whale inventory snapshots used for strict SELL mirroring
- `market_metadata_cache`: Gamma-derived market metadata

## Long-Lived Tasks Started From `main()`

At a high level:

- scanner loop
- websocket tracker
- polling fallback
- copy engine
- balance cache
- risk-state refresh
- periodic reconcile
- dashboard
- price updater
- resolution watcher
- stale cleanup
- heartbeat
- latency monitor
- daily summary

If you change shared state, assume one of these loops can observe it concurrently.

## Known High-Risk Areas

These are the places where agents should be most careful:

- live-mode fill handling vs draft-order assumptions
- SELL sizing and strict inventory mirroring
- divergence between SQLite and `InMemoryState`
- websocket reconnect plus polling overlap plus dedup correctness
- stale or incomplete whale inventory
- partial failures that leave `trade_signals` or `copy_trades` misleading
- live-vs-paper behavior gaps hidden by `paper_perfect_mirror`

## Working Rules For Agents

If your task touches the trade path, do these things by default:

1. Read the full path, not just the target file.
2. Check both DB and RAM state transitions.
3. Compare paper and live behavior explicitly.
4. Add or update tests for regressions on correctness, not just happy-path behavior.

When changing behavior around orders or fills, trace all of:

- `TradeSignal`
- `copy_trades`
- `bot_positions`
- `whale_inventory`
- `InMemoryState`
- startup reconcile
- any background loop that may race with the same state

## Verification Commands

Use `uv`, not raw `pytest`, unless the environment already guarantees it.

Common commands:

```bash
uv run pytest -q
uv run pytest tests/test_signal_detector.py -q
uv run pytest tests/test_position_sync.py -q
uv run pytest tests/test_final_sweep.py -q
```

If you add behavior in the execution path, prefer adding a focused regression test first.

## Current Implementation Priorities

If multiple options look plausible, bias toward the ones that reduce these risks first:

1. actual fill truth in live mode
2. exact SELL mirroring
3. continuous whale inventory correctness
4. atomic DB/RAM state transitions
5. dedup and overlap correctness
6. accounting-grade risk state

## Token-Efficient Orientation

If you are freshly spawned and need fast context, read in this order:

1. this file
2. [main.py](/Users/erik/Work/polyclone/main.py)
3. [src/tracker/trade_monitor.py](/Users/erik/Work/polyclone/src/tracker/trade_monitor.py)
4. [src/tracker/signal_detector.py](/Users/erik/Work/polyclone/src/tracker/signal_detector.py)
5. [src/executor/copy_engine.py](/Users/erik/Work/polyclone/src/executor/copy_engine.py)
6. [src/executor/position_manager.py](/Users/erik/Work/polyclone/src/executor/position_manager.py)
7. the most relevant tests in [tests/](/Users/erik/Work/polyclone/tests)

If the code and README disagree, trust the code and update docs if your task depends on the difference.
