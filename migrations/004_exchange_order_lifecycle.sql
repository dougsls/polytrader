-- 004_exchange_order_lifecycle.sql
-- Live mode: exchange events become the only source of truth for order
-- lifecycle and fills. We persist open orders separately so restart
-- reconciliation can recover submitted/partial orders without creating
-- phantom positions from optimistic local fills.

ALTER TABLE copy_trades ADD COLUMN submitted_at TEXT;
ALTER TABLE copy_trades ADD COLUMN updated_at TEXT;

CREATE INDEX IF NOT EXISTS idx_copy_trades_order_id ON copy_trades(order_id);

CREATE TABLE IF NOT EXISTS copy_trade_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    copy_trade_id TEXT NOT NULL,
    order_id TEXT NOT NULL UNIQUE,
    condition_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'submitted',
    submitted_at TEXT NOT NULL,
    last_update_at TEXT NOT NULL,
    matched_size REAL NOT NULL DEFAULT 0.0,
    avg_price REAL,
    error TEXT,
    raw_event_json TEXT,
    FOREIGN KEY (copy_trade_id) REFERENCES copy_trades(id)
);

CREATE INDEX IF NOT EXISTS idx_copy_trade_orders_trade_id
    ON copy_trade_orders(copy_trade_id);
CREATE INDEX IF NOT EXISTS idx_copy_trade_orders_status
    ON copy_trade_orders(status);

CREATE TABLE IF NOT EXISTS copy_trade_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    copy_trade_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    fill_id TEXT NOT NULL,
    size REAL NOT NULL,
    price REAL NOT NULL,
    filled_at TEXT NOT NULL,
    raw_event_json TEXT,
    UNIQUE(order_id, fill_id),
    FOREIGN KEY (copy_trade_id) REFERENCES copy_trades(id)
);

CREATE INDEX IF NOT EXISTS idx_copy_trade_fills_trade_id
    ON copy_trade_fills(copy_trade_id);
CREATE INDEX IF NOT EXISTS idx_copy_trade_fills_order_id
    ON copy_trade_fills(order_id);
