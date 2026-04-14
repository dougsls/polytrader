# PolyTrader Production Readiness and Profitability Plan

## Goal

Turn the current repo into a production-grade trading system that is:

- operationally reliable under live market conditions
- execution-correct at the fill and position-accounting level
- observable and auditable end to end
- safe enough to run with real capital
- profitable only after measured validation, not by assumption

Profitability is a validation target, not a promise. The implementation goal is to create a bot that can **measure, preserve, and compound edge** after fees, slippage, and operational failure modes.

## Scope

This plan covers:

- strategy correctness and execution fidelity
- data capture, backtesting, and shadow-mode evaluation
- production infrastructure, security, and operations
- staged rollout from paper to limited live to scaled live
- measurement framework for expected value, drawdown, and execution quality

This plan does **not** assume the current copy-trading strategy is already profitable. It assumes the strategy must earn the right to capital through evidence.

## Constraints

- Current tests pass, but green tests are not enough for live trading.
- The current system is stronger in `paper` mode than in `live` mode.
- Live fills are not fully reconciled from exchange truth today.
- Wallet discovery is currently a curated static seed list, not true ongoing discovery.
- Market behavior can change faster than code can be updated.
- Jurisdiction, exchange access, and compliance constraints must be treated as blocking inputs.
- Risk of capital loss must be assumed throughout.

## Current-State Assessment

### What is already good

- Clear async orchestration and module boundaries
- SQLite-backed event and state persistence
- Risk gating, slippage checks, duration filters, and dashboard/metrics surfaces
- Working tests for major internal behaviors
- Clean separation between scanner, tracker, executor, and notifier

### What is not yet production-ready

- Live fills are inferred from draft orders instead of reconciled from actual exchange fill events.
- Partial fills and order lifecycle truth are not first-class in the runtime state model.
- Paper mode defaults are unrealistically permissive and can hide live-only failure modes.
- Tag exposure protection appears inconsistent with the cached market schema.
- Daily PnL / halt logic is approximate rather than accounting-grade.
- Discovery of profitable wallets is only semi-dynamic.
- There is no demonstrated research pipeline proving durable positive expected value after costs.

## Risk Tier

- `R3`

Trading real capital on partially trusted execution/accounting logic is high risk. Profitability work is also high risk because a false positive can look good in paper mode and fail in live trading.

## Verification Level

- `critical`

This work needs unit, integration, replay, shadow-mode, and staged live verification before capital scale-up.

## Success Criteria

The bot is only considered production-ready when all of the following are true:

- Actual exchange fills, cancellations, and partial fills are captured and persisted as source of truth.
- Bot positions, cash, realized PnL, and unrealized PnL reconcile to exchange truth at all times or trigger automated incident state.
- Risk controls fail closed on stale balance, stale market metadata, missing fills, or broken reconciliation.
- Shadow mode shows positive expected value over a statistically meaningful sample after fees and slippage.
- Limited-capital live mode reproduces shadow-mode assumptions within defined tolerance.
- Operational incidents can be detected, triaged, and recovered without manual database repair.

Recommended minimum profitability gate before scale-up:

- at least 30 days of shadow mode on real-time live signals
- at least 2 weeks of limited-capital live mode
- positive net PnL after fees and slippage
- max drawdown within predefined capital policy
- no unresolved execution-accounting discrepancies

## Decision Gate

The following choices are blocking and must be decided before execution starts:

1. **Strategy scope**
   - Recommended: keep v1 focused on copy-trading, but add a hybrid scoring layer that decides *when not to copy*.
   - Alternative: build a broader autonomous market-making / prediction strategy. This is a different product and should not be mixed into this plan.

2. **Capital policy**
   - Recommended: define explicit rollout caps for `paper`, `shadow`, `pilot-live`, and `scaled-live`.
   - Example: `$0`, `$0`, `$100-$250`, then scale only after acceptance gates.

3. **Compliance / exchange access**
   - Recommended: treat jurisdiction, exchange terms, and funding method as blocking prerequisites.
   - No implementation should assume production live trading is permissible until this is settled.

4. **Latency posture**
   - Recommended: production host near exchange edge with a repeatable provisioning path and measured RTT budget.
   - If latency cannot be kept within budget, strategy assumptions must be reworked.

## Commit Plan

- **Commit 1: Execution truth and state correctness**
  - Wire actual fill lifecycle into runtime state.
  - Introduce first-class order status and partial-fill accounting.

- **Commit 2: Accounting, reconciliation, and risk hardening**
  - Correct PnL, exposure, balance freshness, and halt semantics.
  - Add automatic invariant checks and fail-closed behavior.

- **Commit 3: Market metadata and portfolio constraints**
  - Fix tag/category persistence and exposure bucketing.
  - Tighten market schema normalization and validation.

- **Commit 4: Research and replay pipeline**
  - Capture raw events, fills, order-book snapshots, and decision reasons.
  - Build deterministic replay and backtest inputs from production data.

- **Commit 5: Strategy and wallet-selection improvements**
  - Replace static-seed dependence with ranked discovery and ongoing scoring.
  - Add decision features for copy/no-copy gating.

- **Commit 6: Production operations and security**
  - Harden deployment, secrets, alerts, and runbooks.
  - Add staged environment separation and incident procedures.

- **Commit 7: Staged rollout and capital ramp**
  - Add shadow mode, pilot-live mode, and capital policy enforcement.
  - Encode go/no-go gates in config and operator workflow.

## Implementation Steps

### 1. Make live execution accounting-grade

Deliverables:

- Consume the private/user exchange websocket or equivalent live order/fill stream.
- Model order lifecycle explicitly: submitted, partially filled, filled, canceled, rejected, expired.
- Persist actual executed size, actual executed price, fill timestamps, and cancellation reason.
- Update `bot_positions` from actual fills only, not from intended draft values.
- Reconcile order state with exchange truth on startup and periodically during runtime.

Acceptance criteria:

- A partial fill no longer overstates position size.
- A canceled order no longer creates a phantom position.
- DB, in-memory state, and exchange truth converge after restart.
- A full replay of order lifecycle can reconstruct final positions exactly.

### 2. Harden the accounting and risk model

Deliverables:

- Correct `daily_pnl`, realized PnL, unrealized PnL, and drawdown calculations.
- Make balance staleness, reconciliation drift, and missing metadata explicit halt conditions.
- Add invariant checks such as:
  - open position size must equal cumulative fills minus cumulative sells
  - realized PnL must match closed-lot accounting
  - portfolio value must reconcile with cash plus marked positions
- Fail closed on invariant breach and notify operator immediately.

Acceptance criteria:

- Risk gates operate on accounting truth, not approximations.
- Dashboard and Telegram numbers match DB-derived accounting.
- Reconciliation drift is either zero or explicitly surfaced as a production incident.

### 3. Fix portfolio constraints and market metadata quality

Deliverables:

- Separate market tags/categories from token lists in cached market metadata.
- Normalize Gamma market payloads into a stable internal schema.
- Ensure exposure bucketing uses real persisted tags.
- Add metadata freshness and schema-integrity checks.
- Tighten duration, resolution, and market-status gating for live execution.

Acceptance criteria:

- Tag concentration limits work on real runtime data.
- No BUY order bypasses exposure protection because of malformed metadata.
- Markets with missing or stale critical metadata fail closed.

### 4. Build a research-grade data and replay layer

Deliverables:

- Persist raw RTDS payloads, polling payloads, Gamma snapshots, order-book reads, decisions, orders, and fills.
- Add structured decision reasons for every skipped or executed trade.
- Build deterministic replay from captured production-like data.
- Add post-trade attribution:
  - signal edge
  - slippage cost
  - latency cost
  - miss cost
  - risk-filter effect

Acceptance criteria:

- Any live trade can be reconstructed from raw input to final PnL.
- Replay of a captured session produces identical decision outcomes.
- Strategy changes can be evaluated on historical captured sessions before release.

### 5. Replace “copy everything eligible” with measured edge selection

Deliverables:

- Upgrade wallet selection from static curation plus enrichment to a discovery and ranking pipeline.
- Add wallet-level features:
  - rolling PnL after costs
  - fill quality proxy
  - market specialization
  - exit discipline
  - latency sensitivity
  - crowding / copyability
- Add market-level features:
  - liquidity/spread regime
  - time-to-resolution bands
  - event category
  - order-book resiliency after whale trades
- Add a decision layer that can reject otherwise valid copy signals when expected edge is too low.

Acceptance criteria:

- Every live/paper decision includes a measurable expected-value rationale.
- Strategy evaluation compares naive copy-trading vs gated copy-trading.
- Selection quality is tracked by cohort and regime, not only aggregate PnL.

### 6. Add production operations, security, and recovery discipline

Deliverables:

- Environment separation for local, paper, shadow, pilot-live, and scaled-live.
- Secrets management beyond plain `.env` on the target host.
- Alerting for:
  - no fills
  - drift detected
  - stale balance
  - stale websocket
  - repeated order rejection
  - elevated slippage
  - unusual skip spikes
- Runbooks for:
  - restart
  - reconcile
  - exchange outage
  - dashboard lockout
  - corrupted local state
  - funding issues
- Periodic backups of the SQLite database and captured event logs.

Acceptance criteria:

- An operator can diagnose and recover the top failure modes without code changes.
- Secrets and funding keys are no longer handled as an ad hoc VPS-only concern.
- Recovery steps are documented and rehearsed.

### 7. Roll out through strict capital gates

Deliverables:

- Add `shadow` mode that consumes real signals and simulates realistic fills from live book conditions rather than idealized whale fills.
- Keep `paper` mode, but make it explicit whether it is optimistic or realistic.
- Add pilot-live mode with fixed low capital caps and additional alert thresholds.
- Define scale-up criteria based on sample size, drawdown, slippage, and reconciliation quality.

Recommended rollout:

- **Stage A: realistic shadow**
  - No capital deployed
  - Compare decisions to live market truth and whale outcomes

- **Stage B: pilot live**
  - Very small capital cap
  - No overnight unattended scaling
  - Manual daily review required

- **Stage C: guarded live**
  - Higher caps only after quantitative acceptance
  - Continue shadow comparison in parallel

Acceptance criteria:

- Each stage has explicit entry and exit gates.
- Capital cannot be scaled by operator discretion alone without passing checks.

## Tests

### Unit tests

- Partial fill accounting
- Order cancellation and rejection handling
- Reconciliation drift detection
- Tag exposure on real cached market schema
- Daily PnL and drawdown correctness
- Halt triggers on stale balance, stale fills, and invariant breach

### Integration tests

- Startup with preexisting open orders and partial fills
- Restart during order lifecycle
- Websocket disconnect and recovery
- Exchange rejection / retry behavior
- Dashboard and metrics consistency against DB truth

### Replay / simulation tests

- Deterministic replay of captured live sessions
- Compare optimistic paper vs realistic shadow outcomes
- Evaluate strategy changes on fixed historical captures before release

### Acceptance scenarios

- Whale BUY copied with correct live fill accounting
- Whale SELL resized correctly against actual held inventory
- Market resolves while position is open
- Order partially fills then gets canceled
- Reconciliation repairs local drift without manual DB edits
- Risk halt freezes trading and surfaces cause immediately

## Metrics to Track

### Reliability

- signal ingestion lag
- signal-to-order latency
- order-to-fill latency
- websocket uptime
- reconciliation drift count
- stale-balance incidents
- exchange reject rate

### Execution quality

- intended price vs actual fill price
- actual fill size vs intended size
- slippage vs whale anchor
- spread at signal time vs fill time
- missed-fill rate

### Strategy quality

- gross PnL
- net PnL after fees/slippage
- PnL by wallet cohort
- PnL by market category
- PnL by hours-to-resolution band
- Sharpe-like stability metric
- max drawdown
- hit rate of filtered vs unfiltered signals

## Recommended Defaults

- Keep v1 strategy as copy-trading with a stronger “do not copy” layer.
- Treat realistic shadow mode as mandatory before any live scale.
- Disable optimistic paper mirroring by default once realistic shadow exists.
- Prefer fail-closed over fail-open on missing market metadata, stale balance, or fill ambiguity.
- Keep SQLite for v1 if event volume remains manageable, but move raw event capture to append-only files or a dedicated event store if load grows.

## Assumptions

- The operator wants a profitable trading system, not just a technically correct bot.
- The current repo remains the base rather than being replaced by a new architecture.
- Production deployment remains single-node at first, with optional later extraction of data/research components.
- Profitability will be proven by measured live-like evidence, not inferred from current passing tests.

## QA Handoff

- Mode: `fix-and-verify`
- Preferred surfaces:
  - `/health`
  - `/metrics`
  - `/api/overview`
  - `/api/signals`
  - `/api/positions`
  - `/api/trades`
- Required checks:
  - dashboard numbers match DB-derived accounting
  - partial-fill scenarios are reflected correctly in positions and PnL
  - halt/resume works under real incident conditions
  - restart and reconciliation recover without phantom positions
  - shadow-mode reports expose expected-value and execution-cost attribution

## First Execution Order

If implementation starts immediately, the first three workstreams should be:

1. **Execution truth**
   - wire actual fills into state and persistence
   - eliminate draft-as-fill assumptions

2. **Accounting correctness**
   - fix PnL, drawdown, and reconciliation invariants
   - make risk fail closed on ambiguity

3. **Research and shadow mode**
   - capture raw events and decisions
   - build realistic shadow evaluation before touching live scale

Anything outside those three is secondary until the bot can be trusted to know what it actually bought, sold, and earned.
