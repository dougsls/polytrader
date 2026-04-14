# PolyTrader

**Autonomous Polymarket copy-trading bot.** It identifies profitable wallets on the leaderboard, monitors their trades in real time via WebSocket, and automatically replicates positions 24/7 with strict risk controls.

Python 3.12+ · 100% async · 46 tests · clean `ruff` · SQLite with WAL · lean stack (~80 deps)

> ⚠️ **Warning.** Copy-trading prediction markets involves real risk of capital loss. This project is infrastructure, not financial advice. Run in `paper` mode for 7-14 days before going `live`, and even in live mode start with 10% of your target capital. Read [Safety and Operating Modes](#safety-and-operating-modes).

---

## Table of Contents

1. [What the bot does](#what-the-bot-does)
2. [Architecture](#architecture)
3. [Operating regime](#operating-regime)
4. [The 3 market microstructure laws](#the-3-market-microstructure-laws)
5. [The 4 HFT directives](#the-4-hft-directives)
6. [Performance](#performance)
7. [Infrastructure](#infrastructure)
8. [Installation](#installation)
9. [Configuration](#configuration)
10. [Go-live](#go-live)
11. [Safety and operating modes](#safety-and-operating-modes)
12. [Project structure](#project-structure)
13. [Tests](#tests)
14. [Useful commands](#useful-commands)
15. [Troubleshooting](#troubleshooting)
16. [License and disclaimer](#license-and-disclaimer)

---

## What the bot does

PolyTrader runs three continuous, parallel activities:

### 1. **Scanner**: discovers who to copy

Every hour (configurable), it pulls the **leaderboard** from the Polymarket Data API for multiple periods (7d, 30d), scores each wallet using a composite score (`pnl`, `win_rate`, `consistency`, `recency`, `market diversification`, `% of trades in short-duration markets`), and maintains a **ranked top-N wallet pool**. Wallets that underperform or become inactive are automatically replaced. Wallets that show a **wash-trading** pattern (high volume, tiny PnL, typical of Polygon _airdrop farmers_) get `score=0` and never enter the pool.

### 2. **Tracker**: watches their trades

It keeps a persistent WebSocket connection to Polymarket RTDS (`wss://ws-live-data.polymarket.com`) listening to the `activity/trades` topic. Each packet is filtered using a `Set[str]` of tracked wallets in **nanoseconds**: packets from unknown wallets are discarded before any processing. When it detects a trade from a wallet in the pool, it creates a `TradeSignal` and pushes it into an `asyncio.Queue`.

### 3. **Executor**: replicates positions

It consumes the signal queue and, for each signal:

- Applies the **Risk Manager checklist** (minimum score, price band, max positions, daily loss, drawdown, portfolio cap).
- Verifies **Law 1 (Anti-Slippage Anchoring)** by reading the CLOB order book.
- For SELL, validates **Law 2 (Exit Syncing)** against the in-memory cache of the bot's own positions.
- Quantizes price to the asset `tick_size` (**Directive 1**) and routes through the correct exchange if the market is `neg_risk` (**Directive 4**).
- Sends a GTC order with a price offset to compensate for the ~100 ms NY→London RTT. If it does not fill within 30s, it falls back to FOK.
- Updates `bot_positions` and the in-memory cache, then notifies via Telegram.

### 4. **Notifier**: tells you what matters

Telegram fire-and-forget: executed trades, skipped trades with reasons, risk/latency/geoblock alerts, and a daily summary at 22:00 UTC.

---

## Architecture

```text
┌───────────────────────────────────────────────────────────────────┐
│                            POLYTRADER                              │
│                                                                    │
│  ┌─────────────┐     ┌─────────────┐     ┌──────────────────┐    │
│  │   SCANNER   │     │   TRACKER   │     │    EXECUTOR      │    │
│  │             │     │             │     │                  │    │
│  │ Leaderboard │     │ WS RTDS     │     │ Risk Manager     │    │
│  │ Profiler    │     │ (Set filter)│ ──> │ Slippage Anchor  │    │
│  │ Scorer      │ ──> │ Dedup       │     │ Order Manager    │    │
│  │ WalletPool  │     │ SignalQueue │     │ Position Manager │    │
│  └─────────────┘     └─────────────┘     └──────────────────┘    │
│        │                    │                     │              │
│        ▼                    ▼                     ▼              │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │        INFRASTRUCTURE (shared async resources)           │    │
│  │  httpx AsyncClient (pooled)  │  aiosqlite conn (WAL)     │    │
│  │  InMemoryState (RAM cache)   │  BalanceCache (15s bg)    │    │
│  │  structlog JSON              │  Gamma cache (5min TTL)   │    │
│  │  HeartbeatWatchdog           │  orjson (zero-copy parse) │    │
│  └─────────────────────────────────────────────────────────┘    │
│        │                                          │              │
│        ▼                                          ▼              │
│  ┌─────────────┐                         ┌───────────────┐      │
│  │  NOTIFIER   │                         │   DASHBOARD   │      │
│  │  (Telegram) │                         │   (FastAPI)   │      │
│  └─────────────┘                         └───────────────┘      │
└───────────────────────────────────────────────────────────────────┘
```

### Per-signal flow (critical path)

```text
Trade on Polymarket
    │
    ▼
RTDS WebSocket  ──────────  orjson parse (Law 4)
    │
    ▼ Set[str] filter (maker ∈ tracked_wallets?)
    │
    ▼ (drop in ns if not)
TradeMonitor.dedup
    │
    ▼
detect_signal (~78 μs median)
    ├─ age filter
    ├─ minimum USD size filter
    ├─ gamma.get_market (5min cache)
    ├─ duration filter (>48h = HARD BLOCK)
    └─ Law 2: if SELL, checks RAM state cache
    │
    ▼ TradeSignal → asyncio.Queue
CopyEngine.handle_signal
    ├─ RiskManager.evaluate (10-item checklist)
    ├─ Law 1: check_slippage_or_abort (best_ask > whale*1.03?)
    ├─ build_draft (Directive 1 quantizes + Directive 4 neg_risk)
    ├─ CLOB.post_order (Directive 2 retry on 425)
    └─ apply_fill → write-through bot_positions + RAM state
    │
    ▼
TelegramNotifier.notify_trade
```

---

## Operating regime

### Allowed markets

| Filter                         | Default value              | Reason                                                     |
| ------------------------------ | -------------------------- | ---------------------------------------------------------- |
| **Maximum** time to resolution | 48h (hard block at 3 days) | Capital locked in weeks/months-long events is unacceptable |
| **Minimum** time to resolution | 6min                       | Closing markets do not fill orders reliably                |
| Preferred range                | 1h - 24h                   | Sweet spot: good liquidity, predictable volatility         |
| Indefinite resolution          | **REJECT**                 | No end date means unlimited risk                           |

**Examples:**
| Market | Action |
|---|---|
| "Hawks vs Celtics tonight?" (4h) | ✅ COPY |
| "Fed rate decision tomorrow?" (22h) | ✅ COPY |
| "Bitcoin $110k by Friday?" (48h) | ✅ COPY (limit) |
| "Trump impeached by July?" (2160h) | 🚫 HARD BLOCK |
| "Will it rain in NYC tomorrow?" (~3min) | 🚫 TOO CLOSE |

### Wallets eligible for copying

Every gate below must pass before the score even matters:

- PnL over the period ≥ **$500** (configurable)
- Win rate ≥ **55%**
- At least **10 trades** in the period
- At least **2 distinct markets** (single-market wallet can be insider noise)
- **≥ 50% of trades in short-duration markets** (< 48h)
- `|pnl| / volume ≥ 0.05`: **anti-wash-trading filter** (Law 3)

### Order sizing

Three configurable modes:

- `fixed`: always N USD per order
- `proportional`: `portfolio_value × proportional_factor` (default: 5%)
- `kelly`: simplified Kelly using `wallet_score` as an edge proxy

Always respecting `max_position_usd` and `max_portfolio_usd`.

---

## The 3 market microstructure laws

These rules are not tips. They are **survival invariants** in a low-liquidity CLOB. Violating them is like turning on an HFT trader without circuit breakers.

### Law 1: Anti-Slippage Anchoring

**Problem:** when the whale buys, it consumes liquidity. In ~100 ms (the NY→London RTT), the `best_ask` may already be 5-10% above the whale's execution price. If the bot copies using the current midpoint, it becomes exit liquidity for an euphoric market.

**Solution:** before signing any order, read the CLOB `/book`. If `best_ask > whale_execution_price × 1.03` (3%, configurable), **ABORT**. Implementation: [src/executor/slippage.py](src/executor/slippage.py).

### Law 2: Exit Syncing (Strict Inventory Mirroring)

**Problem:** if the whale sells `YES`, copying that SELL only makes sense if the bot has `YES` to sell. Naive bots sell blindly and end up _short_ in a market that does not allow shorting, or they place an order that is always rejected.

**Solution:** the tracker keeps an **in-memory inventory** of followed wallets (synced by polling `/positions`), and the executor only accepts SELL if `bot_positions` contains the token. Sell size is **proportional** to the % the whale sold:

```text
pct_sold = min(whale_sell_size / whale_prior_size, 1.0)
bot_sell_size = bot_holdings × pct_sold
```

Implementation: [src/core/state.py](src/core/state.py) + [src/tracker/signal_detector.py](src/tracker/signal_detector.py).

### Law 3: Eliminate Wash Traders

**Problem:** Polygon airdrop farmers make 10,000 fake trades against their own orders. They show up near the top of the leaderboard with inflated metrics. Copying a wash trader is guaranteed loss because they are trading against themselves.

**Solution:** compute `|PnL| / volume`. A wallet that moves $100,000 to make $500 has a ratio of 0.005, which means it is **not a high achiever, just noise**. Wallets with ratio < 0.05 get `score=0` in [src/scanner/scorer.py](src/scanner/scorer.py).

---

## The 4 HFT directives

These complement the laws above at the execution-infrastructure level.

### Directive 1: Price Quantization and Tick Size

Polymarket rejects (HTTP 400) prices that are not aligned to the asset `tick_size`. Rounding with float `round()` fails silently because of ULP error (`0.1 + 0.2 ≠ 0.3`). We use `Decimal` with `ROUND_HALF_EVEN`.
Implementation: [src/core/quantize.py](src/core/quantize.py) + [src/api/order_builder.py](src/api/order_builder.py).

### Directive 2: Exponential Backoff for HTTP 425

Polymarket's matching engine periodically restarts quickly and returns `425 Too Early`. Retry is deterministic: **1.5s → 3.0s → 6.0s**, maximum 4 attempts, only for 425 (other 4xx errors are propagated). Decorator in [src/api/retry.py](src/api/retry.py).

### Directive 3: Heartbeat Watchdog (Cancel-on-Disconnect)

If the WS L2 CLOB goes silent, Polymarket **cancels all open limit orders** automatically. An async watchdog fires `postHeartbeat` every 10s; after N consecutive failures, it forces a stream reconnect.
Implementation: [src/api/heartbeat.py](src/api/heartbeat.py).

### Directive 4: Negative Risk Routing

Multi-outcome markets (for example, "Who wins the election?" with 5+ candidates) use Polymarket's `neg_risk` exchange, which applies netting across complementary positions (Σ prices = 1.0). Orders in these markets need the correct adapter and header. `CLOBClient._pick_signer()` routes automatically based on the `neg_risk` flag persisted in `market_metadata_cache`.
Implementation: [src/api/clob_client.py](src/api/clob_client.py) + [src/api/auth.py](src/api/auth.py).

Bonus: **orjson + Set filtering**. RTDS emits up to 3k msgs/s during spikes. Native `json` chokes CPU; Rust-backed `orjson` is about 3× faster. Before forwarding anything, we check whether `maker ∈ tracked_wallets` (`Set` O(1)), so 99% of packets are dropped in nanoseconds.

---

## Performance

The project went through autonomous optimization driven by [autoresearch](https://github.com/karpathy/autoresearch):

| Metric                 | Baseline (P2) | After optimization |  Speedup |
| ---------------------- | ------------: | -----------------: | -------: |
| `detect_signal` mean   |     25,654 μs |          **78 μs** | **329×** |
| `detect_signal` median |     24,520 μs |          **71 μs** | **345×** |
| `rtds_parse_filter`    |      45.63 μs |           43.75 μs |   stable |
| **Total hot path**     | **25,700 μs** |         **140 μs** | **184×** |

### At 3k msgs/s (documented RTDS peak):

- **Before:** 77s of CPU per second of stream, total saturation, impossible to operate
- **After:** 0.21s of CPU per second, about 15% of 1 core, with 6× headroom

### The 5 optimizations that mattered

1. **Shared SQLite connection** (iter 6, -93%): one `aiosqlite.Connection` for the lifetime of the process, injected into the tracker. Each open cost ~10 ms of thread init on Windows.
2. **In-memory fast path** (Phase 4, -82%): `InMemoryState` with `bot_positions_by_token` + `whale_inventory` in dicts. Exit Syncing lookup becomes sub-μs.
3. **Merged lookups on the same connection** (iter 1, -52%): `bot_positions` and `whale_inventory` queried using the same connection.
4. **Function inlining + `now` reuse** (iter 3, -10%): removed one `datetime.now()` call and one function call.
5. **`TradeSignal.model_construct`** (iter 8): data produced by our own code does not need Pydantic revalidation.

Full details in [autoresearch/260413-2014-polytrader-perf/summary.md](autoresearch/260413-2014-polytrader-perf/summary.md).

---

## Infrastructure

### Production server: QuantVPS, New York

| Spec        | Value                       |
| ----------- | --------------------------- |
| OS          | Ubuntu 24.04 LTS (headless) |
| Minimum RAM | 8 GB                        |
| Storage     | NVMe SSD                    |
| Network     | 1 Gbps (burst 10 Gbps)      |
| Uptime SLA  | 99.999%                     |
| DDoS        | included                    |

### NY → London latency (where the CLOB runs)

Polymarket's matching engine runs in **AWS eu-west-2 (London)**. Typical RTT:

| Route                 | RTT           |
| --------------------- | ------------- |
| NY → London (CLOB/WS) | **70-130 ms** |
| Dublin → London       | 0-2 ms        |
| London → London       | < 1 ms        |

**Why does NY still work?** We are not doing HFT against market makers. The edge is **replicating profitable traders**, not beating the order book on raw speed. 130 ms after the whale trade, price usually has not moved too far in short-duration markets. We compensate with GTC _limit orders_ using a 2% offset plus FOK fallback.

### Geoblock (US IPs)

International Polymarket blocks US IPs for trading. The bot checks `https://polymarket.com/api/geoblock` at startup; if blocked and `exchange_mode=international`, it fails fast. External routing (VPN/proxy) is the operator's responsibility.

---

## Installation

### Prerequisites

- Python 3.12+
- [uv](https://astral.sh/uv) (package manager)
- SQLite (already available on macOS/Linux)
- Polymarket account with a configured wallet (MetaMask or similar)
- Bridged USDC.e on Polygon in the _funder_ wallet
- Telegram bot + chat ID (optional, but recommended)

### Local development

```bash
git clone https://github.com/dougsls/polytrader.git
cd polytrader
uv sync                                   # creates .venv + installs deps
uv run python scripts/init_db.py          # applies SQLite schema
uv run python -m pytest -q                # 46 tests in ~2s
```

### VPS (Ubuntu 24.04)

```bash
git clone https://github.com/dougsls/polytrader.git /opt/polytrader
cd /opt/polytrader
bash scripts/provision_vps.sh             # UTC timezone, deps, uv, UFW, systemd enable
```

After provisioning:

```bash
sudo nano /opt/polytrader/.env            # fill PRIVATE_KEY, FUNDER_ADDRESS, TELEGRAM_*
uv run python scripts/geoblock_check.py   # IP ok?
uv run python scripts/latency_test.py     # RTT < 150ms p95?
uv run python scripts/setup_wallet.py     # derive L2 + verify USDC.e balance
sudo systemctl start polytrader
sudo journalctl -u polytrader -f          # live logs
```

---

## Configuration

Two layers: `.env` (secrets) + `config.yaml` (behavior).

### `.env`: see [.env.example](.env.example)

| Variable                     | Description                                                                        |
| ---------------------------- | ---------------------------------------------------------------------------------- |
| `PRIVATE_KEY`                | Private key for the trading wallet. **Never commit it.**                           |
| `FUNDER_ADDRESS`             | Proxy address (if `SIGNATURE_TYPE=1/2`); otherwise same as the private key address |
| `SIGNATURE_TYPE`             | `0`=EOA, `1`=Email/Magic, `2`=Browser wallet                                       |
| `EXCHANGE_MODE`              | `international` (`clob.polymarket.com`) or `us` (CFTC, invite-only)                |
| `TELEGRAM_BOT_TOKEN`         | Telegram bot token for alerts                                                      |
| `TELEGRAM_CHAT_ID`           | Destination chat ID (negative for groups)                                          |
| `LATENCY_ALERT_THRESHOLD_MS` | Alert if RTT > N ms (default 200)                                                  |

### `config.yaml`: main nodes

```yaml
scanner:
  max_wallets_tracked: 20
  min_profit_usd: 500
  min_win_rate: 0.55
  wash_trading_filter:
    min_volume_to_pnl_ratio: 0.05 # Law 3

tracker:
  market_duration_filter:
    max_hours_to_resolution: 48 # markets <= 48h
    hard_block_days: 3 # >3 days = NEVER

executor:
  mode: "live" # live | paper | dry-run
  max_portfolio_usd: 500
  max_position_usd: 100
  max_positions: 15
  max_daily_loss_usd: 100
  max_drawdown_pct: 0.20
  whale_max_slippage_pct: 0.03 # Law 1: abort if best_ask > whale*1.03
  limit_price_offset: 0.02 # GTC with 2% midpoint offset
  fok_fallback_timeout_seconds: 30 # fallback to FOK after 30s
  min_confidence_score: 0.6 # only copy wallets with score >= 0.6
```

---

## Go-live

### Recommended sequence (do not skip steps)

**Week 0: Preparation**

1. Provision QuantVPS in NY, run `provision_vps.sh`
2. Configure `.env` with real credentials
3. `geoblock_check.py` → not blocked
4. `latency_test.py` → p95 < 150 ms
5. `setup_wallet.py` → L2 derived + USDC.e balance confirmed

**Week 1-2: Paper trading** 6. Set `executor.mode: "paper"` in `config.yaml` 7. `sudo systemctl start polytrader` 8. Observe for 7-14 days: detected signals, applied filters, theoretical PnL, slippage abort rate, market-duration block rate

**Week 3: Gradual live** 9. Set `mode: "live"` + `max_portfolio_usd: 50` (10% of target) 10. `sudo systemctl restart polytrader` 11. Monitor for 3-5 days before scaling

**Week 4+: Scale** 12. As win rate and real drawdown converge toward paper results, gradually increase `max_portfolio_usd` until target

### Emergency rollback

```bash
sudo systemctl stop polytrader            # stops immediately
# open orders remain on Polymarket; cancel them manually in the UI if needed
```

---

## Safety and operating modes

### Three modes

- `dry-run`: detects signals, applies filters, but **does not build orders**. Used to validate the pipeline.
- `paper`: detects signals, builds `OrderDraft`, records `CopyTrade`, and updates `bot_positions`, **but does not send to the CLOB**. Used to measure theoretical PnL with realistic friction.
- `live`: sends real orders via `py-clob-client` + EIP-712 signatures. **Only enable after paper mode.**

### Risk gates (block any trade)

- Source wallet score < `min_confidence_score`
- Outcome price outside `[min_price, max_price]`
- Open positions ≥ `max_positions`
- Daily loss exceeded `max_daily_loss_usd`
- Drawdown reached `max_drawdown_pct`
- Portfolio + proposed position > `max_portfolio_usd`

Any failed gate means the signal is marked `skipped` with an auditable `skip_reason` in `trade_signals.skip_reason`.

### Global halt

When `daily_loss` or `drawdown` is exceeded, the RiskManager sets `is_halted=True` and **rejects every subsequent signal** until the operator intervenes manually.

### Secrets

- `PRIVATE_KEY` is never persisted to DB or logs
- `L2Credentials` live only in RAM (derived once at startup)
- `.gitignore` excludes `.env` and `data/*.db*`
- Dashboard (optional) is protected by `DASHBOARD_SECRET`

---

## Project structure

```text
polytrader/
├── main.py                           # asyncio.gather orchestrator
├── pyproject.toml                    # uv + 19 runtime deps + 6 dev deps
├── config.yaml                       # bot behavior
├── .env.example                      # secrets template
│
├── src/
│   ├── core/
│   │   ├── config.py                 # pydantic-settings + YAML loader
│   │   ├── database.py               # SQLite WAL + migrations runner
│   │   ├── exceptions.py             # PolyTraderError tree
│   │   ├── logger.py                 # structlog JSON -> journalctl
│   │   ├── models.py                 # Pydantic v2 models
│   │   ├── quantize.py               # Directive 1 (Decimal)
│   │   └── state.py                  # Phase 4: InMemoryState RAM cache
│   │
│   ├── api/
│   │   ├── auth.py                   # EOA L2 prefetch
│   │   ├── balance.py                # on-chain USDC.e via web3
│   │   ├── clob_client.py            # CLOB + retry_on_425
│   │   ├── data_client.py            # Data API (leaderboard/positions/trades)
│   │   ├── gamma_client.py           # Gamma API + market_metadata_cache
│   │   ├── heartbeat.py              # Directive 3 watchdog
│   │   ├── http.py                   # httpx AsyncClient singleton
│   │   ├── order_builder.py          # Directives 1+4 applied
│   │   ├── retry.py                  # Directive 2 (@retry_on_425)
│   │   ├── startup_checks.py         # geoblock + latency baseline
│   │   └── websocket_client.py       # RTDS + orjson + Set filter (Law 4)
│   │
│   ├── scanner/
│   │   ├── leaderboard.py            # fetch + multi-period aggregation
│   │   ├── profiler.py               # WalletProfile + V/PnL ratio
│   │   ├── scorer.py                 # Law 3 (wash filter) + composite score
│   │   └── wallet_pool.py            # persisted top-N + live Set for RTDS
│   │
│   ├── tracker/
│   │   ├── signal_detector.py        # Law 2 (Exit Syncing) + duration filter
│   │   ├── trade_monitor.py          # consume RTDS + dedup + enqueue
│   │   └── whale_inventory.py        # snapshot /positions -> state
│   │
│   ├── executor/
│   │   ├── balance_cache.py          # 15s background refresh
│   │   ├── copy_engine.py            # central pipeline (risk -> slippage -> post)
│   │   ├── order_manager.py          # build_draft + persist CopyTrade
│   │   ├── position_manager.py       # apply_fill + write-through state
│   │   ├── risk_manager.py           # 10-item checklist + halt
│   │   └── slippage.py               # Law 1 (Anti-Slippage Anchoring)
│   │
│   └── notifier/
│       └── telegram.py               # async fire-and-forget
│
├── tests/                            # 46 pytest tests + pytest-benchmark
│   ├── test_scorer.py                # numerical proof of Law 3
│   ├── test_signal_detector.py       # proof of Law 2 + duration filter
│   ├── test_slippage.py              # proof of Law 1 BUY/SELL
│   ├── test_risk_manager.py          # gates + halt
│   ├── test_state_cache.py           # RAM fast path
│   ├── test_wallet_pool.py           # ranking + in-place Set mutation
│   ├── test_retry.py                 # Directive 2 (425 backoff)
│   ├── test_heartbeat.py             # Directive 3 (watchdog)
│   ├── test_quantize.py              # Directive 1 (Decimal ULP)
│   ├── test_order_builder.py         # Directives 1+4
│   ├── test_gamma_cache.py           # TTL cache
│   ├── test_config.py                # YAML + .env loader
│   └── benchmark.py                  # pytest-benchmark hot paths
│
├── migrations/
│   ├── 001_initial.sql               # 9 tables + indexes
│   └── 002_hft_resilience.sql        # tick_size + neg_risk columns
│
├── scripts/
│   ├── init_db.py                    # applies migrations
│   ├── geoblock_check.py             # checks blocked IP
│   ├── latency_test.py               # 10 probes × 3 targets (p50/p95)
│   ├── setup_wallet.py               # derives L2 + checks balance
│   └── provision_vps.sh              # one-shot QuantVPS setup
│
├── deploy/
│   └── polytrader.service            # systemd unit (watchdog 120s)
│
└── autoresearch/                     # autonomous optimization log
    └── 260413-2014-polytrader-perf/
        ├── overview.md
        ├── results.tsv               # 10 iterations
        └── summary.md                # documented 56× speedup
```

---

## Tests

```bash
uv run python -m pytest -q                           # 46 tests in ~2s
uv run python -m pytest tests/test_slippage.py -v   # proof of Law 1
uv run python -m pytest tests/benchmark.py -q       # hot-path benchmarks
```

Coverage of the laws:
| Rule / Directive | Test file |
|---|---|
| Law 1 (Slippage) | `test_slippage.py` |
| Law 2 (Exit Syncing) | `test_signal_detector.py`, `test_state_cache.py` |
| Law 3 (Wash Trading) | `test_scorer.py` |
| Law 4 (orjson+Set) | `benchmark.py::test_bench_rtds_parse_filter` |
| Directive 1 (Quantize) | `test_quantize.py`, `test_order_builder.py` |
| Directive 2 (425 Backoff) | `test_retry.py` |
| Directive 3 (Heartbeat) | `test_heartbeat.py` |
| Directive 4 (`neg_risk`) | `test_order_builder.py` |

---

## Useful commands

### Daily operation (VPS)

```bash
sudo systemctl status polytrader
sudo systemctl restart polytrader
sudo journalctl -u polytrader -f                                # live logs
sudo journalctl -u polytrader -f | jq 'select(.level=="error")' # errors only
sudo journalctl -u polytrader --since "1 hour ago" | jq .
```

### Diagnostics

```bash
uv run python scripts/geoblock_check.py       # exit 0 = ok, 1 = blocked
uv run python scripts/latency_test.py         # p50/p95 RTT
sqlite3 data/polytrader.db "SELECT * FROM tracked_wallets WHERE is_active=1;"
sqlite3 data/polytrader.db "SELECT * FROM trade_signals ORDER BY detected_at DESC LIMIT 20;"
sqlite3 data/polytrader.db "SELECT * FROM risk_snapshots ORDER BY timestamp DESC LIMIT 1;"
```

### DB backup

```bash
# SQLite is a single file: schedule a daily cron backup
cp data/polytrader.db "data/polytrader_$(date +%Y%m%d).db"
```

---

## Troubleshooting

### "EOA auth prefetch failed"

Check whether `PRIVATE_KEY` in `.env` is valid (hex `0x...` with 64 chars). Run `scripts/setup_wallet.py` on its own for detailed debugging.

### "geoblock_detected"

Your IP is in the US or in another blocked region. Options:

- Move the VPS to Europe/Asia
- Use an outbound proxy (operator responsibility)
- Wait for Polymarket US (CFTC) to become publicly available

### Persistent latency > 200ms

This may be a QuantVPS network issue at that moment. Check:

```bash
uv run python scripts/latency_test.py
mtr --report clob.polymarket.com
```

If it remains above that for > 30 min, open a ticket with QuantVPS.

### Bot restarts in a loop

Get the latest error:

```bash
sudo journalctl -u polytrader --since "10 min ago" | grep -i error | tail -20
```

Common causes: malformed `.env`, invalid `PRIVATE_KEY`, wrong type in `config.yaml` (Pydantic validates strictly at startup).

### Signals arrive but all become `skipped`

Check the reason in `trade_signals.skip_reason`:

```sql
SELECT skip_reason, COUNT(*) FROM trade_signals
WHERE status='skipped' AND detected_at > datetime('now','-1 hour')
GROUP BY skip_reason;
```

Common causes: minimum score set too high, price band too narrow, all pool wallets trading in markets > 48h.

---

## License and disclaimer

**Use at your own risk.** This code is provided "AS IS", without warranties. Operating in prediction markets involves real risk of total capital loss. The author and contributors are not responsible for financial losses, regulatory consequences, or any other damage arising from the use of this infrastructure.

Before operating in `live`, make sure that you:

- Know the laws in your country regarding prediction markets
- Understand the Polymarket model (CTF Exchange + `neg_risk` adapter)
- Have tested in `paper` for at least 7 days
- Are comfortable losing 100% of the allocated capital

If you do not understand a section of this README, **do not run live**.

---

## Acknowledgements

- **Polymarket** for the public API and `py-clob-client`.
- **Andrej Karpathy** for the [autoresearch](https://github.com/karpathy/autoresearch) pattern that guided the autonomous performance optimization.
- **QuantVPS** for the NY datacenter with solid SLA and DDoS protection.
