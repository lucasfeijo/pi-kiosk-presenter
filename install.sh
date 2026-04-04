#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/pi-display-server"
SERVICE_NAME="pi-display-server"

echo "=== Pi Display Server — Installer ==="

# --- Dependencies ----------------------------------------------------------
echo "[1/4] Installing system dependencies…"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    xdotool \
    x11-utils \
    mpv \
    chromium \
    feh \
    python3

# --- Copy files ------------------------------------------------------------
echo "[2/4] Installing server to ${INSTALL_DIR}…"
sudo mkdir -p "${INSTALL_DIR}"
sudo cp "$(dirname "$0")/display_server.py" "${INSTALL_DIR}/display_server.py"
sudo chmod +x "${INSTALL_DIR}/display_server.py"

# --- Systemd service -------------------------------------------------------
echo "[3/4] Installing systemd service…"
sudo cp "$(dirname "$0")/${SERVICE_NAME}.service" "/etc/systemd/system/${SERVICE_NAME}.service"

# Update the User= line to match the current user
CURRENT_USER="$(whoami)"
sudo sed -i "s/^User=.*/User=${CURRENT_USER}/" "/etc/systemd/system/${SERVICE_NAME}.service"
sudo sed -i "s|/home/pi/|/home/${CURRENT_USER}/|g" "/etc/systemd/system/${SERVICE_NAME}.service"

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"

# --- Start -----------------------------------------------------------------
echo "[4/4] Starting service…"
sudo systemctl start "${SERVICE_NAME}"

echo ""
echo "Done! The server is running on port 8686."
echo ""
echo "Quick test:"
echo "  curl http://$(hostname -I | awk '{print $1}'):8686/status"
echo ""
echo "Manage with:"
echo "  sudo systemctl status ${SERVICE_NAME}"
echo "  sudo systemctl restart ${SERVICE_NAME}"
echo "  journalctl -u ${SERVICE_NAME} -f"
