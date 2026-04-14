-- 003_close_reason.sql
-- Distingue como a posição fechou: 'sold' (whale vendeu, bot copiou)
-- vs 'resolved' (mercado decidiu o outcome via Polymarket).
-- Sem isso, dashboard mostra todas em "Resolvidas" com PnL=$0
-- quando whale entra/sai rápido pelo mesmo preço.

ALTER TABLE bot_positions ADD COLUMN close_reason TEXT;
