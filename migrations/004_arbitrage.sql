-- migrations/004_arbitrage.sql
-- Track A — pure-arbitrage engine (YES+NO < 1 edge).
-- Tabelas separadas das de copy-trade pra não poluir queries do
-- dashboard/risk_state e permitir PnL/banca isolados.

-- Oportunidades detectadas pelo scanner. UMA linha = 1 condition_id em
-- 1 timestamp. Idempotente em (condition_id, detected_at) — múltiplos
-- scans do mesmo book tick não duplicam.
CREATE TABLE IF NOT EXISTS arb_opportunities (
    id TEXT PRIMARY KEY,                      -- uuid
    condition_id TEXT NOT NULL,
    yes_token_id TEXT NOT NULL,
    no_token_id TEXT NOT NULL,
    market_title TEXT,
    -- Snapshot dos asks (best ask de cada outcome)
    ask_yes REAL NOT NULL,
    ask_no REAL NOT NULL,
    -- Profundidade USD encontrada em cada side (até max_levels)
    depth_yes_usd REAL,
    depth_no_usd REAL,
    -- Edge bruto = 1 - (ask_yes + ask_no)
    edge_gross_pct REAL NOT NULL,
    -- Edge líquido = bruto - fees - safety_buffer
    edge_net_pct REAL NOT NULL,
    -- Tamanho recomendado em USD por leg (limitado pelo book + per_op_usd)
    suggested_size_usd REAL NOT NULL,
    -- Hours-to-resolution snapshot (filtros de duração)
    hours_to_resolution REAL,
    detected_at TEXT NOT NULL,
    -- Status: pending, executed, skipped, expired, failed
    status TEXT NOT NULL DEFAULT 'pending',
    skip_reason TEXT
);

-- Execuções: 1 op = 2 ordens CLOB + 1 merge on-chain.
-- Atomic = se leg2 falha, leg1 é desfeita (vendida) ou anulada.
CREATE TABLE IF NOT EXISTS arb_executions (
    id TEXT PRIMARY KEY,                      -- uuid
    opportunity_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    -- Capital efetivo investido (soma das 2 fills)
    invested_usd REAL,
    -- Order IDs CLOB
    yes_order_id TEXT,
    no_order_id TEXT,
    yes_fill_price REAL,
    no_fill_price REAL,
    yes_fill_size REAL,
    no_fill_size REAL,
    yes_status TEXT,                          -- filled, partial, failed, cancelled
    no_status TEXT,
    -- Merge on-chain: tx hash da chamada mergePositions
    merge_tx_hash TEXT,
    merge_status TEXT,                        -- pending, confirmed, failed, skipped
    merge_amount_usd REAL,
    -- PnL realizado: merge devolve $1 por par; receita = $1 * size_par - invested
    realized_pnl_usd REAL,
    -- Estado global da op: completed, partial, failed, rolled_back
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (opportunity_id) REFERENCES arb_opportunities(id)
);

-- Snapshot de banca isolada da arb (independente do copy-trader).
CREATE TABLE IF NOT EXISTS arb_bank_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    capital_usd REAL NOT NULL,
    realized_pnl_usd REAL NOT NULL DEFAULT 0,
    open_ops INTEGER NOT NULL DEFAULT 0,
    completed_ops_total INTEGER NOT NULL DEFAULT 0,
    failed_ops_total INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_arb_opps_condition ON arb_opportunities(condition_id);
CREATE INDEX IF NOT EXISTS idx_arb_opps_status ON arb_opportunities(status);
CREATE INDEX IF NOT EXISTS idx_arb_opps_detected ON arb_opportunities(detected_at);
CREATE INDEX IF NOT EXISTS idx_arb_exec_opportunity ON arb_executions(opportunity_id);
CREATE INDEX IF NOT EXISTS idx_arb_exec_status ON arb_executions(status);
CREATE INDEX IF NOT EXISTS idx_arb_exec_started ON arb_executions(started_at);
CREATE INDEX IF NOT EXISTS idx_arb_bank_timestamp ON arb_bank_snapshots(timestamp);
