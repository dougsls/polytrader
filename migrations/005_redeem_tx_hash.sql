-- migrations/005_redeem_tx_hash.sql
--
-- ⚠️ FUND-LOCK FIX
--
-- bot_positions ganha coluna redeem_tx_hash. Quando o resolution_watcher
-- detecta won=True, dispara CTF.redeemPositions on-chain pra trocar o
-- token vencedor (1 USDC face value) por USDC real. Sem esse saque, o
-- dinheiro fica preso no contrato CTF para sempre.
--
-- Estados possíveis:
--   NULL                  → ainda não dispatched (próximo tick re-tenta)
--   "dispatching"         → task em vôo (sentinel pra evitar race entre ticks)
--   "0x<hex>"             → tx confirmada on-chain
--   "paper-skip"          → mode != live; sem chamada on-chain
--   "lost-skip"           → posição perdedora; redeem desperdiçaria gas
--   "failed:<msg>"        → falha; será retried no próximo tick
ALTER TABLE bot_positions ADD COLUMN redeem_tx_hash TEXT;

CREATE INDEX IF NOT EXISTS idx_positions_redeem_tx ON bot_positions(redeem_tx_hash);
