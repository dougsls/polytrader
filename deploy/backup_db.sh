#!/bin/bash
# Backup do DB SQLite a cada 6h. Mantém últimos 28 backups (7 dias).
# Instalar:
#   sudo cp deploy/backup_db.sh /usr/local/bin/polytrader_backup.sh
#   sudo chmod +x /usr/local/bin/polytrader_backup.sh
#   sudo crontab -e
#   # adicione:
#   0 */6 * * * /usr/local/bin/polytrader_backup.sh

set -e
BACKUP_DIR=/opt/polytrader/backups
SRC=/opt/polytrader/data/polytrader.db
mkdir -p "$BACKUP_DIR"

# Backup atomico via .backup do sqlite3 (consistente mesmo com bot rodando)
TS=$(date +%Y%m%d_%H%M%S)
sqlite3 "$SRC" ".backup '$BACKUP_DIR/polytrader_$TS.db'"

# Mantém só os 28 mais recentes (7 dias × 4 backups/dia)
ls -t "$BACKUP_DIR"/polytrader_*.db 2>/dev/null | tail -n +29 | xargs -r rm -f

echo "[$(date)] Backup OK: polytrader_$TS.db ($(du -h $BACKUP_DIR/polytrader_$TS.db | cut -f1))"
