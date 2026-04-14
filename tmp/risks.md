**Executive Summary**
`uv run pytest -q` is green (`86 passed`), but the suite mostly covers helpers and tracker fragments, not the live execution contract. The highest-risk problems are all correctness issues: live mode records fills it has not observed, the executor discards the tracker’s mirrored SELL quantity, whale inventory is not kept fresh after startup, and multiple paths can leave SQLite and `InMemoryState` disagreeing. I would treat the top two as live-trading blockers.

**Top Issues Ranked By Severity**

1. Critical: live mode treats `post_order` acceptance as a full fill.  
   Why it matters: your system of record and hot cache become wrong the moment an accepted order is partial, resting, cancelled, or price-improved.  
   Files/functions: [src/executor/copy_engine.py](/Users/erik/Work/polyclone/src/executor/copy_engine.py:187) `CopyEngine.handle_signal`, [src/executor/position_manager.py](/Users/erik/Work/polyclone/src/executor/position_manager.py:26) `apply_fill`, [src/api/user_ws_client.py](/Users/erik/Work/polyclone/src/api/user_ws_client.py:1) `UserWSClient` is present but unused.  
   Failure mode: after `post_order()` returns, the code immediately calls `apply_fill()` with draft size/price, marks `copy_trades` filled, and updates RAM. `order_id` is never persisted, so you cannot reconcile partial fills or watchdog reposts correctly.  
   Likely trigger: any live `GTC` that rests, partially fills, gets cancelled, or fills at a different price; any reconnect after a successful post.  
   Concrete fix: persist `copy_trades.status='submitted'` plus `order_id` first; only call `apply_fill()` on confirmed fill events or authoritative order-status reconciliation; wire `UserWSClient` into live startup and support `partial/filled/cancelled`.

2. Critical: the proportional SELL size computed by exit-sync is discarded before execution.  
   Why it matters: this directly breaks the repo’s stated “strict inventory mirroring” rule.  
   Files/functions: [src/tracker/signal_detector.py](/Users/erik/Work/polyclone/src/tracker/signal_detector.py:129) `detect_signal`, [src/executor/risk_manager.py](/Users/erik/Work/polyclone/src/executor/risk_manager.py:85) `_size_usd`, [src/executor/order_manager.py](/Users/erik/Work/polyclone/src/executor/order_manager.py:54) `build_draft`.  
   Failure mode: `detect_signal()` computes `signal.size = bot_size * pct_sold`, but `build_draft()` ignores `signal.size` and rebuilds token size from `decision.sized_usd / price`. I reproduced this locally: a SELL signal of `25.0` tokens became a draft of `1.25` tokens under current config.  
   Likely trigger: every SELL, especially with `whale_proportional`, `fixed`, or `proportional` sizing.  
   Concrete fix: SELLs should use exact token quantity from `signal.size` (clamped to current bot inventory after quantization). Keep risk as a gate, not a SELL resizer.

3. High: whale inventory is not maintained after initial wallet onboarding, so SELL mirroring runs on stale or missing whale state.  
   Why it matters: strict exit mirroring only works if prior whale inventory is continuously updated.  
   Files/functions: [src/scanner/scanner.py](/Users/erik/Work/polyclone/src/scanner/scanner.py:105) `Scanner.tick`, [src/tracker/whale_inventory.py](/Users/erik/Work/polyclone/src/tracker/whale_inventory.py:21) `snapshot_whale`, [src/tracker/signal_detector.py](/Users/erik/Work/polyclone/src/tracker/signal_detector.py:139) SELL path.  
   Failure mode: snapshots only run for `newly_added` wallets. Later whale BUYs/SELLs never update `whale_inventory`, and missing tokens are never deleted on full snapshot. New whale positions after startup will later skip SELL with `exit_sync_no_whale_snapshot`; topped-up positions will sell the wrong fraction.  
   Likely trigger: any whale that opens a new position after startup, adds to an existing one, or fully exits between scans.  
   Concrete fix: move whale inventory maintenance out of scanner warm-up; either run a dedicated `/positions` refresh loop with delete-diff semantics, or apply raw trade deltas transactionally from the feed and persist the new whale baseline immediately.

4. High: `InMemoryState` and SQLite are not updated atomically, and some DB writers never update RAM at all.  
   Why it matters: SELL safety depends on RAM being a correct mirror; right now it can get ahead of DB, behind DB, or briefly empty.  
   Files/functions: [src/executor/position_manager.py](/Users/erik/Work/polyclone/src/executor/position_manager.py:43) `apply_fill`, [src/tracker/whale_inventory.py](/Users/erik/Work/polyclone/src/tracker/whale_inventory.py:46) `snapshot_whale`, [src/core/state.py](/Users/erik/Work/polyclone/src/core/state.py:76) `reload_from_db`, [src/executor/resolution_watcher.py](/Users/erik/Work/polyclone/src/executor/resolution_watcher.py:101) `_resolve_position`.  
   Failure mode: `apply_fill()` and `snapshot_whale()` mutate RAM before DB commit. I reproduced `apply_fill()` leaving `state.bot_size('t1') == 10.0` after a DB failure. `resolution_watcher` closes positions in SQLite but never fixes RAM. `reload_from_db()` clears both dicts before awaiting DB reads, so periodic reconcile can expose a temporary empty-state window to `detect_signal()`.  
   Likely trigger: DB exceptions, schema mismatch, periodic reconcile, resolution watcher closing a position, or any mid-write crash.  
   Concrete fix: write DB first inside an explicit transaction, mutate RAM only after commit, and make reload build fresh dicts off-thread/off to the side and swap references atomically.

5. High: stale-position cleanup is broken end to end and would currently fail at the DB boundary.  
   Why it matters: a “safety cleanup” path that cannot actually submit a valid trade is worse than no cleanup, because it creates false confidence.  
   Files/functions: [src/executor/stale_cleanup.py](/Users/erik/Work/polyclone/src/executor/stale_cleanup.py:63) `stale_position_cleanup_loop`, [src/executor/order_manager.py](/Users/erik/Work/polyclone/src/executor/order_manager.py:82) `build_draft`, [migrations/001_initial.sql](/Users/erik/Work/polyclone/migrations/001_initial.sql:31).  
   Failure mode: stale cleanup enqueues a synthetic `TradeSignal`, but never inserts it into `trade_signals`. `build_draft()` then inserts `copy_trades(signal_id=...)` under FK enforcement and fails. I reproduced this locally: `IntegrityError FOREIGN KEY constraint failed`.  
   Likely trigger: any position older than `max_position_age_hours`.  
   Concrete fix: send stale-cleanup signals through the same persist-before-enqueue path as tracker signals, and add an idempotent reservation flag so the same open position cannot generate repeated synthetic SELLs.

6. High: dedup, overlap, and backpressure handling can both drop real trades and create duplicate execution windows while leaving misleading audit rows.  
   Why it matters: this is your main protection against websocket reconnect replay and polling overlap.  
   Files/functions: [src/tracker/trade_monitor.py](/Users/erik/Work/polyclone/src/tracker/trade_monitor.py:59) `_dedup_key`, [src/tracker/trade_monitor.py](/Users/erik/Work/polyclone/src/tracker/trade_monitor.py:109) `_enqueue_trade`, [main.py](/Users/erik/Work/polyclone/main.py:404) task startup, [src/tracker/signal_detector.py](/Users/erik/Work/polyclone/src/tracker/signal_detector.py:216) hardcoded source.  
   Failure mode: the dedup key is only `(wallet, condition, token, side, int(ts))`, so same-bucket distinct trades can collapse. If a message has no timestamp, fallback `time.monotonic_ns()` guarantees replayed duplicates will not dedup. Keys are remembered before persistence succeeds, so transient failures suppress retries. If the queue is full, the dropped signal stays `pending` in `trade_signals`. Polling signals are also persisted as `source='websocket'`.  
   Likely trigger: websocket reconnect replay, polling/websocket overlap, same-second burst fills, queue saturation, transient Gamma/DB failures. Uncertainty: the exact severity depends on live timestamp precision, but the missing-timestamp path is definitely duplicate-prone.  
   Concrete fix: use exchange event ids when available; otherwise include size/price in the fingerprint; only mark “seen” after successful persistence; mark dropped rows explicitly as dropped; pass the actual source through the model; honor `tracker.use_websocket` instead of always starting both paths.

7. High: the daily-loss halt ignores realized losses from closed positions.  
   Why it matters: one of the main live capital brakes is currently weaker than it looks.  
   Files/functions: [src/executor/risk_state.py](/Users/erik/Work/polyclone/src/executor/risk_state.py:55) `build_risk_state`, [src/executor/risk_manager.py](/Users/erik/Work/polyclone/src/executor/risk_manager.py:129) `evaluate`.  
   Failure mode: `build_risk_state()` computes `daily_realized` but returns `daily_pnl=unrealized` only. I reproduced `total_realized_pnl=-20.0` with `daily_pnl=0.0`, so `max_daily_loss_usd` would not trip.  
   Likely trigger: any day where losses are realized while unrealized PnL is flat or positive.  
   Concrete fix: set `daily_pnl = daily_realized + unrealized`, add a regression test, and consider separating realized-loss and drawdown halts.

**Missing Tests**

- No meaningful end-to-end tests for [src/executor/copy_engine.py](/Users/erik/Work/polyclone/src/executor/copy_engine.py:111) `handle_signal()` across `paper`, `live`, and `dry-run`.
- No test that a SELL signal’s mirrored token quantity survives the executor and becomes the actual order size.
- No live-path tests for `submitted -> partial -> filled/cancelled` with real `order_id` handling and watchdog fallback.
- No failure-injection tests proving RAM is unchanged when DB writes in `apply_fill()` or `snapshot_whale()` fail.
- No test covering whale inventory evolution after startup: BUY adds, partial SELL reduces, full exit deletes, restart reload stays correct.
- No end-to-end stale-cleanup test through DB persistence, copy-trade creation, and idempotent single-close behavior.
- No overlap tests for websocket replay + polling overlap + queue saturation with correct final `trade_signals.status`.
- No regression test for daily-loss halting on realized losses.

**Refactors Worth Doing Only If They Reduce Risk**

- Split BUY sizing from SELL quantity. BUYs can be USD-sized; SELLs should execute exact token amounts from the mirrored signal.
- Introduce a single execution state machine for `trade_signals` and `copy_trades` with explicit `submitted/partial/filled/cancelled/failed` transitions and persisted `order_id`.
- Replace clear-and-reload RAM mutation with atomic snapshot swap, and make all RAM writes happen post-commit only.
- Either wire or delete dead safety knobs. Right now `copy_buys`, `copy_sells`, `use_websocket`, and `avoid_resolved_markets` are not protecting anything if operators rely on them.

**What I Would Change First This Week**

1. Stop treating live `post_order()` success as a fill. Persist `submitted + order_id`, wire fill confirmation, and disable live mode until that exists.
2. Fix the SELL path so `signal.size` is the executed quantity, then add a single end-to-end regression test that proves strict exit mirroring.
3. Build a real whale-inventory updater with delete semantics and immediate post-trade baseline updates.
4. Make RAM updates post-commit only and make `reload_from_db()` swap snapshots atomically.
5. Disable stale cleanup until it goes through the normal signal persistence path and has idempotency protection.
