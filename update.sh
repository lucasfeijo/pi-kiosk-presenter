#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/pi-display-server"
SERVICE_NAME="pi-display-server"
BACKEND="${DISPLAY_BACKEND:-x11}"

cd "${INSTALL_DIR}"

echo "Pulling latest…"
sudo git pull
sudo chown -R "$(whoami)" "${INSTALL_DIR}"

if [ "${BACKEND}" = "wayland" ]; then
    if ! command -v swaymsg >/dev/null 2>&1; then
        echo "Missing dependency (sway). Installing…"
        sudo apt-get update -qq
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq sway swaybg wlr-randr imv
    fi
else
    if ! command -v xdpyinfo >/dev/null 2>&1; then
        echo "Missing dependency (xdpyinfo). Installing x11-utils…"
        sudo apt-get update -qq
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq x11-utils
    fi
fi

if ! diff -q "${INSTALL_DIR}/${SERVICE_NAME}.service" "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null 2>&1; then
    echo "Service file changed, updating…"
    sudo cp "${INSTALL_DIR}/${SERVICE_NAME}.service" "/etc/systemd/system/${SERVICE_NAME}.service"
    sudo systemctl daemon-reload
fi

if [ -n "${DISPLAY_BACKEND:-}" ] && grep -q '^Environment=DISPLAY_BACKEND=' "/etc/systemd/system/${SERVICE_NAME}.service" 2>/dev/null; then
    echo "Setting DISPLAY_BACKEND=${DISPLAY_BACKEND} in systemd unit…"
    sudo sed -i "s/^Environment=DISPLAY_BACKEND=.*/Environment=DISPLAY_BACKEND=${DISPLAY_BACKEND}/" \
        "/etc/systemd/system/${SERVICE_NAME}.service"
    sudo systemctl daemon-reload
fi

echo "Restarting service…"
sudo systemctl restart "${SERVICE_NAME}"
echo "Done — $(date)"
