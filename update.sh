#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/pi-display-server"
SERVICE_NAME="pi-display-server"

cd "${INSTALL_DIR}"

echo "Pulling latest…"
sudo git pull
sudo chown -R "$(whoami)" "${INSTALL_DIR}"

if ! command -v xdpyinfo >/dev/null 2>&1; then
    echo "Missing dependency detected (xdpyinfo). Installing x11-utils…"
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq x11-utils
fi

if ! diff -q "${INSTALL_DIR}/${SERVICE_NAME}.service" "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null 2>&1; then
    echo "Service file changed, updating…"
    sudo cp "${INSTALL_DIR}/${SERVICE_NAME}.service" "/etc/systemd/system/${SERVICE_NAME}.service"
    sudo systemctl daemon-reload
fi

echo "Restarting service…"
sudo systemctl restart "${SERVICE_NAME}"
echo "Done — $(date)"
