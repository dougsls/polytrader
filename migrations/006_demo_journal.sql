-- migrations/006_demo_journal.sql
--
-- 24h DEMO REALISTA — auditoria de cada decisão de execução.
--
-- Cada signal que entra no executor gera UMA linha aqui, independente
-- do desfecho. Permite calcular slippage real, missed opportunities,
-- avoided losses, comparar whale vs bot, atribuir PnL a wallets.
--
-- Diferente de copy_trades (que só tem trades executados), o journal
-- captura TODOS os signals processados — inclusive skips e partials.

CREATE TABLE IF NOT EXISTS demo_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,                 -- ISO8601 UTC

    -- Sinal de origem
    signal_id TEXT NOT NULL,
    wallet_address TEXT NOT NULL,
    wallet_score REAL,
    confluence_count INTEGER NOT NULL DEFAULT 1,

    -- Mercado / token
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    market_title TEXT,
    outcome TEXT,
    side TEXT NOT NULL,                       -- BUY / SELL

    -- Preços de comparação
    whale_price REAL,                         -- preço executado pela whale
    whale_size REAL,                          -- size em tokens da whale
    whale_usd REAL,                           -- USD value do trade da whale
    ref_price REAL,                           -- best ask (BUY) / best bid (SELL)
    simulated_price REAL,                     -- VWAP do fill simulado
    spread REAL,                              -- ask - bid
    slippage REAL,                            -- (simulated_price/whale_price)-1

    -- Sizing
    intended_size REAL,                       -- bot pretendia tradear
    intended_size_usd REAL,
    simulated_size REAL,                      -- bot conseguiu encher (tokens)
    simulated_size_usd REAL,
    depth_usd REAL,                           -- depth disponível no book
    levels_consumed INTEGER,

    -- Status final
    status TEXT NOT NULL,                     -- executed / partial / skipped / failed
    skip_reason TEXT,
    is_emergency_exit INTEGER NOT NULL DEFAULT 0,  -- 1 se stale/bypass

    -- PnL (atualizado a posteriori quando posição fecha)
    expected_pnl REAL,                        -- estimado no momento (whale_pnl proxy)
    realized_pnl REAL,
    unrealized_pnl REAL,

    -- Latência & origem
    source TEXT,                              -- websocket / polling / stale_cleanup
    latency_signal_to_decision_ms REAL,
    latency_signal_to_fill_ms REAL,

    -- Metadados extras (JSON serializado)
    extras_json TEXT,

    FOREIGN KEY (signal_id) REFERENCES trade_signals(id)
);

CREATE INDEX IF NOT EXISTS idx_demo_journal_timestamp ON demo_journal(timestamp);
CREATE INDEX IF NOT EXISTS idx_demo_journal_status ON demo_journal(status);
CREATE INDEX IF NOT EXISTS idx_demo_journal_wallet ON demo_journal(wallet_address);
CREATE INDEX IF NOT EXISTS idx_demo_journal_skip_reason ON demo_journal(skip_reason);
CREATE INDEX IF NOT EXISTS idx_demo_journal_condition ON demo_journal(condition_id);


-- Snapshots periódicos da banca demo. 1 row a cada N segundos.
-- Usado pra equity curve, drawdown ao longo do tempo, exposição.
CREATE TABLE IF NOT EXISTS demo_bank_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,

    -- Banca
    starting_bank_usd REAL NOT NULL,
    cash_available REAL,
    invested_usd REAL,
    market_value_usd REAL,                    -- σ size × current_price
    bank_total REAL,                          -- cash + market_value

    -- PnL
    realized_pnl REAL,
    unrealized_pnl REAL,
    total_pnl REAL,
    roi_pct REAL,

    -- Drawdown
    peak_bank REAL,
    current_drawdown_pct REAL,
    max_drawdown_pct REAL,

    -- Contadores agregados
    open_positions INTEGER,
    signals_total INTEGER,
    signals_executed INTEGER,
    signals_skipped INTEGER,
    signals_partial INTEGER,
    signals_dropped INTEGER,
    win_count INTEGER,
    loss_count INTEGER,

    -- Health
    rtds_connected INTEGER,
    polling_active INTEGER,
    geoblock_status TEXT,
    queue_size INTEGER
);

CREATE INDEX IF NOT EXISTS idx_demo_bank_timestamp ON demo_bank_snapshots(timestamp);


-- Snapshots de readiness score (0-100) + flags de NO-GO.
CREATE TABLE IF NOT EXISTS demo_readiness_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    score INTEGER NOT NULL,                   -- 0-100
    recommendation TEXT NOT NULL,             -- GO / NO-GO / EXTEND_PAPER

    -- Componentes (cada um aporta pro score)
    roi_component INTEGER,
    drawdown_component INTEGER,
    slippage_component INTEGER,
    audit_component INTEGER,
    api_health_component INTEGER,
    skip_rate_component INTEGER,
    sample_size_component INTEGER,
    pnl_concentration_component INTEGER,

    -- Flags NO-GO automático
    drawdown_no_go INTEGER NOT NULL DEFAULT 0,
    state_divergence_no_go INTEGER NOT NULL DEFAULT 0,
    fake_fills_no_go INTEGER NOT NULL DEFAULT 0,
    api_errors_no_go INTEGER NOT NULL DEFAULT 0,
    slippage_no_go INTEGER NOT NULL DEFAULT 0,
    pnl_outlier_no_go INTEGER NOT NULL DEFAULT 0,

    -- Detalhes JSON serializados pra auditoria
    details_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_demo_readiness_ts ON demo_readiness_snapshots(timestamp);


-- Health snapshots de APIs externas (CLOB, Gamma, Data API, RTDS).
-- Permite mostrar latência média/máxima na tela do dashboard.
CREATE TABLE IF NOT EXISTS demo_api_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    target TEXT NOT NULL,                     -- clob / gamma / data_api / rtds_ws
    rtt_ms REAL,
    status_code INTEGER,
    healthy INTEGER NOT NULL DEFAULT 1,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_demo_api_ts_target
    ON demo_api_health(timestamp, target);
