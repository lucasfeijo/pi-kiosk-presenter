#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/pi-display-server"
SERVICE_NAME="pi-display-server"
REPO_URL="${1:-}"

echo "=== Pi Display Server — Installer ==="

if [ -z "${REPO_URL}" ]; then
    echo "Usage: bash install.sh <git-clone-url>"
    echo "  e.g. bash install.sh https://github.com/you/pi-display-server.git"
    exit 1
fi

# --- Dependencies ----------------------------------------------------------
echo "[1/6] Installing system dependencies…"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    git \
    xserver-xorg \
    xinit \
    openbox \
    xdotool \
    x11-utils \
    mpv \
    chromium \
    feh \
    python3

# --- Clone repo ------------------------------------------------------------
echo "[2/6] Cloning repo to ${INSTALL_DIR}…"
if [ -d "${INSTALL_DIR}/.git" ]; then
    echo "  Repo already exists, pulling latest…"
    cd "${INSTALL_DIR}"
    sudo git pull
else
    sudo git clone "${REPO_URL}" "${INSTALL_DIR}"
fi

# --- Symlink update command ------------------------------------------------
echo "[3/6] Installing update-display command…"
sudo chmod +x "${INSTALL_DIR}/update.sh"
sudo ln -sf "${INSTALL_DIR}/update.sh" /usr/local/bin/update-display

# --- Systemd service -------------------------------------------------------
echo "[4/6] Installing systemd service…"
sudo cp "${INSTALL_DIR}/${SERVICE_NAME}.service" "/etc/systemd/system/${SERVICE_NAME}.service"

CURRENT_USER="$(whoami)"
sudo sed -i "s/^User=.*/User=${CURRENT_USER}/" "/etc/systemd/system/${SERVICE_NAME}.service"
sudo sed -i "s|/home/pi/|/home/${CURRENT_USER}/|g" "/etc/systemd/system/${SERVICE_NAME}.service"

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"

# --- Boot files (only if missing) ------------------------------------------
echo "[5/6] Setting up boot files…"

if [ ! -f "$HOME/.xinitrc" ]; then
    cat > "$HOME/.xinitrc" << 'XINITRC'
xset -dpms
xset s off
xset s noblank
xrandr -o right

sudo systemctl restart pi-display-server &

exec openbox-session
XINITRC
    echo "  Created ~/.xinitrc"
else
    echo "  ~/.xinitrc already exists, skipping"
fi

if [ ! -f "$HOME/.bash_profile" ]; then
    cat > "$HOME/.bash_profile" << 'BASHPROFILE'
if [ -z "$DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
  startx
fi
BASHPROFILE
    echo "  Created ~/.bash_profile"
else
    echo "  ~/.bash_profile already exists, skipping"
fi

sudo mkdir -p /etc/systemd/system/getty@tty1.service.d
sudo tee /etc/systemd/system/getty@tty1.service.d/autologin.conf >/dev/null <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin ${CURRENT_USER} --noclear %I \$TERM
EOF
sudo systemctl daemon-reload
echo "  Enabled tty1 autologin for ${CURRENT_USER}"

# --- Start -----------------------------------------------------------------
echo "[6/6] Starting service…"
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
echo ""
echo "To update later:"
echo "  update-display"
