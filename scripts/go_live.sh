#!/bin/bash
# =============================================================
# go_live.sh — Migração paper → live com guard rails
# =============================================================
# Roda na VPS: bash scripts/go_live.sh
# =============================================================

set -e  # aborta no primeiro erro

cd /opt/polytrader

echo "🔍 PRE-FLIGHT CHECKS"
echo "=========================================="

# 1. Confirmar credenciais
[[ -z "$(grep -E '^PRIVATE_KEY=.{60,}' .env)" ]] && {
    echo "❌ PRIVATE_KEY não setado no .env"; exit 1; }
[[ -z "$(grep -E '^FUNDER_ADDRESS=0x.{40}' .env)" ]] && {
    echo "❌ FUNDER_ADDRESS não setado no .env"; exit 1; }
[[ -z "$(grep -E '^TELEGRAM_BOT_TOKEN=.{30,}' .env)" ]] && {
    echo "❌ TELEGRAM_BOT_TOKEN não setado"; exit 1; }
echo "✅ Credenciais ok"

# 2. Confirmar config.live.yaml existe
[[ ! -f config.live.yaml ]] && {
    echo "❌ config.live.yaml não existe"; exit 1; }
echo "✅ config.live.yaml presente"

# 3. Verificar geoblock
GEO=$(curl -s 'https://polymarket.com/api/geoblock' 2>/dev/null || echo '{"blocked":true}')
echo "$GEO" | grep -q '"blocked":true' && {
    echo "❌ IP geo-bloqueado pela Polymarket. Use VPN/proxy."; exit 1; }
echo "✅ Geoblock ok (não bloqueado)"

# 4. Confirmar approvals foram feitas (script setup_wallet.py rodado)
echo ""
echo "⚠️  CONFIRMAÇÃO MANUAL"
echo "=========================================="
echo "Você JÁ rodou 'python scripts/setup_wallet.py' alguma vez? (s/N)"
read -r APPROVED
[[ "$APPROVED" != "s" ]] && {
    echo "❌ Rode 'cd /opt/polytrader && .venv/bin/python scripts/setup_wallet.py' primeiro"
    echo "   (gasta ~\$1 em gas Polygon, aprova USDC e CTF on-chain)"; exit 1; }

echo "Você confirma que tem PELO MENOS \$50 USDC na FUNDER_ADDRESS? (s/N)"
read -r FUNDED
[[ "$FUNDED" != "s" ]] && {
    echo "❌ Deposite USDC primeiro"; exit 1; }

# 5. Backup paper DB
echo ""
echo "📦 BACKUP DB PAPER"
mkdir -p backups
cp data/polytrader.db "backups/polytrader_paper_$(date +%Y%m%d_%H%M%S).db"
echo "✅ Backup salvo em backups/"

# 6. Reset DB pra live
echo ""
echo "🧹 LIMPANDO DB (posições paper não migram)"
sqlite3 data/polytrader.db "DELETE FROM trade_signals; DELETE FROM copy_trades; DELETE FROM bot_positions;"
echo "✅ Tabelas zeradas"

# 7. Trocar config
echo ""
echo "🔄 ATIVANDO config.live.yaml"
cp config.yaml backups/config_paper_$(date +%Y%m%d_%H%M%S).yaml
cp config.live.yaml config.yaml
echo "✅ Config live ativa"

# 8. Restart
echo ""
echo "🔁 RESTART POLYTRADER"
systemctl restart polytrader
sleep 6
STATUS=$(systemctl is-active polytrader)
[[ "$STATUS" != "active" ]] && {
    echo "❌ Service não subiu: $STATUS"
    journalctl -u polytrader -n 30 --no-pager
    exit 1; }
echo "✅ Service active"

# 9. Notificação Telegram
TOKEN=$(grep TELEGRAM_BOT_TOKEN .env | cut -d= -f2)
CHAT=$(grep TELEGRAM_CHAT_ID .env | cut -d= -f2)
curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    -d "chat_id=${CHAT}" \
    -d "text=🚀 PolyTrader migrado pra LIVE. Banca: \$50. Aguardando primeiros sinais." > /dev/null

echo ""
echo "🎉 LIVE ATIVADO"
echo "=========================================="
echo "• Mode:     LIVE"
echo "• Banca:    \$50"
echo "• Cofre:    50% dos lucros"
echo "• Drawdown: pausa em -10%"
echo "• Slippage: max 3% (aborta acima)"
echo "• Spread:   max 2¢ (rejeita ilíquido)"
echo ""
echo "Monitore:"
echo "  • Dashboard: http://\$(curl -s ifconfig.me):8080"
echo "  • Logs:      sudo journalctl -u polytrader -f"
echo "  • Telegram:  alertas no grupo POLY TRADER"
echo ""
echo "ROLLBACK (se algo der errado):"
echo "  cp backups/config_paper_*.yaml config.yaml"
echo "  systemctl restart polytrader"
