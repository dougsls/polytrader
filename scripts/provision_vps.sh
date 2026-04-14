#!/bin/bash
# scripts/provision_vps.sh — Setup inicial da QuantVPS NY (roda uma vez).
#
# Pré-requisitos: Ubuntu 24.04 LTS, root/sudo, rede configurada.
# Uso: bash scripts/provision_vps.sh
set -euo pipefail

echo "=== PolyTrader VPS Setup ==="

# 1. Timezone UTC — OBRIGATÓRIO para timestamps consistentes
sudo timedatectl set-timezone UTC

# 2. Update + dependências de sistema
sudo apt update && sudo apt upgrade -y
sudo apt install -y \
    build-essential \
    python3.12 python3.12-venv python3.12-dev \
    python3-systemd \
    git curl wget sqlite3 ufw htop tmux jq

# 3. uv (package manager)
if ! command -v uv &> /dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    source "$HOME/.local/bin/env"
fi

# 4. Firewall — apenas SSH + dashboard (ajuste porta SSH se não for 22)
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow ssh
sudo ufw allow 8080/tcp
sudo ufw --force enable

# 5. Projeto em /opt/polytrader (assume repo clonado ou rsync prévio)
sudo mkdir -p /opt/polytrader
cd /opt/polytrader

# 6. Ambiente Python
uv venv
uv sync

# 6b. systemd-python (sd_notify no heartbeat) — fora do pyproject pra não
# quebrar dev em Windows/Mac. Aqui compila limpo porque python3-systemd
# já instalou libsystemd-dev acima.
./.venv/bin/pip install "systemd-python>=235" || \
    echo "⚠️  systemd-python pip install falhou — heartbeat não fará WATCHDOG=1 (bot funciona mesmo assim)."

# 7. .env a partir do template — usuário edita depois
if [ ! -f .env ]; then
    cp .env.example .env
    echo "⚠️  EDITE /opt/polytrader/.env com suas credenciais antes de iniciar o serviço."
fi

# 8. Init DB (idempotente)
uv run python scripts/init_db.py

# 9. Systemd service
sudo cp deploy/polytrader.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable polytrader

echo "=== Provisão completa ==="
echo "Próximos passos:"
echo "  1. sudo nano /opt/polytrader/.env    # preencher PRIVATE_KEY, FUNDER_ADDRESS, TELEGRAM_*"
echo "  2. uv run python scripts/geoblock_check.py  # confirma IP não bloqueado"
echo "  3. uv run python scripts/latency_test.py    # confirma RTT < 150ms"
echo "  4. uv run python scripts/setup_wallet.py    # deriva L2 + confere saldo"
echo "  5. sudo systemctl start polytrader          # go-live"
echo "  6. sudo journalctl -u polytrader -f         # logs ao vivo"
