-- migrations/002_hft_resilience.sql
-- Diretivas HFT: persistência de tick_size (Diretiva 1) e neg_risk (Diretiva 4)
-- no cache de metadados de mercado. Consumido por executor (quantização) e
-- clob_client (header neg_risk na construção da ordem).

ALTER TABLE market_metadata_cache ADD COLUMN tick_size REAL;
ALTER TABLE market_metadata_cache ADD COLUMN neg_risk INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
