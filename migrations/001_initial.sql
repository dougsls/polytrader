-- migrations/001_initial.sql
-- PolyTrader initial schema. Applied once at bootstrap.

CREATE TABLE IF NOT EXISTS tracked_wallets (
    address TEXT PRIMARY KEY,
    name TEXT,
    score REAL NOT NULL DEFAULT 0.0,
    pnl_usd REAL NOT NULL DEFAULT 0.0,
    win_rate REAL NOT NULL DEFAULT 0.0,
    total_trades INTEGER NOT NULL DEFAULT 0,
    tracked_since TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    last_trade_at TEXT,
    metadata_json TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_signals (
    id TEXT PRIMARY KEY,
    wallet_address TEXT NOT NULL,
    wallet_score REAL NOT NULL,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    size REAL NOT NULL,
    price REAL NOT NULL,
    usd_value REAL NOT NULL,
    market_title TEXT NOT NULL,
    outcome TEXT NOT NULL,
    market_end_date TEXT,
    hours_to_resolution REAL,
    detected_at TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    skip_reason TEXT,
    FOREIGN KEY (wallet_address) REFERENCES tracked_wallets(address)
);

CREATE TABLE IF NOT EXISTS copy_trades (
    id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    order_id TEXT,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    intended_size REAL NOT NULL,
    executed_size REAL,
    intended_price REAL NOT NULL,
    executed_price REAL,
    slippage REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    filled_at TEXT,
    error TEXT,
    FOREIGN KEY (signal_id) REFERENCES trade_signals(id)
);

CREATE TABLE IF NOT EXISTS bot_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    market_title TEXT NOT NULL,
    outcome TEXT NOT NULL,
    size REAL NOT NULL,
    avg_entry_price REAL NOT NULL,
    current_price REAL,
    unrealized_pnl REAL DEFAULT 0.0,
    realized_pnl REAL DEFAULT 0.0,
    source_wallets_json TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    is_open INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    total_portfolio_value REAL,
    total_invested REAL,
    total_unrealized_pnl REAL,
    total_realized_pnl REAL,
    daily_pnl REAL,
    max_drawdown REAL,
    current_drawdown REAL,
    open_positions INTEGER,
    is_halted INTEGER DEFAULT 0,
    halt_reason TEXT
);

-- Regra 3 (anti-wash-trading): volume_usd registrado para auditoria do V/PnL ratio.
CREATE TABLE IF NOT EXISTS wallet_scores_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL,
    score REAL NOT NULL,
    pnl_usd REAL,
    win_rate REAL,
    total_trades INTEGER,
    volume_usd REAL,
    scored_at TEXT NOT NULL,
    FOREIGN KEY (wallet_address) REFERENCES tracked_wallets(address)
);

CREATE TABLE IF NOT EXISTS market_metadata_cache (
    condition_id TEXT PRIMARY KEY,
    title TEXT,
    slug TEXT,
    end_date TEXT,
    expiration INTEGER,
    active INTEGER,
    closed INTEGER,
    resolved INTEGER,
    tokens_json TEXT,
    fetched_at TEXT NOT NULL,
    ttl_seconds INTEGER NOT NULL DEFAULT 300
);

CREATE TABLE IF NOT EXISTS latency_probes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    target TEXT NOT NULL,
    rtt_ms REAL NOT NULL,
    status_code INTEGER,
    error TEXT,
    vps_location TEXT DEFAULT 'quantvps-ny'
);

-- Regra 2 (Exit Syncing): inventário-espelho das baleias, pollado do /positions.
-- Executor só aprova SELL se bot_positions contém o token; size do SELL é
-- proporcional ao % que a baleia vendeu (delta vs snapshot anterior).
CREATE TABLE IF NOT EXISTS whale_inventory (
    wallet_address TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    size REAL NOT NULL,
    avg_price REAL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (wallet_address, token_id)
);

-- Índices
CREATE INDEX IF NOT EXISTS idx_signals_wallet ON trade_signals(wallet_address);
CREATE INDEX IF NOT EXISTS idx_signals_status ON trade_signals(status);
CREATE INDEX IF NOT EXISTS idx_signals_detected ON trade_signals(detected_at);
CREATE INDEX IF NOT EXISTS idx_signals_hours ON trade_signals(hours_to_resolution);
CREATE INDEX IF NOT EXISTS idx_trades_signal ON copy_trades(signal_id);
CREATE INDEX IF NOT EXISTS idx_trades_status ON copy_trades(status);
CREATE INDEX IF NOT EXISTS idx_positions_open ON bot_positions(is_open);
CREATE INDEX IF NOT EXISTS idx_positions_condition ON bot_positions(condition_id);
CREATE INDEX IF NOT EXISTS idx_risk_timestamp ON risk_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_latency_timestamp ON latency_probes(timestamp);
CREATE INDEX IF NOT EXISTS idx_latency_target ON latency_probes(target);
CREATE INDEX IF NOT EXISTS idx_whale_inv_wallet ON whale_inventory(wallet_address);
