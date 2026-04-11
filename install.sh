#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/pi-display-server"
SERVICE_NAME="pi-display-server"
REPO_URL="${1:-}"
BACKEND="${DISPLAY_BACKEND:-x11}"

echo "=== Pi Display Server — Installer (backend: ${BACKEND}) ==="

if [ -z "${REPO_URL}" ]; then
    echo "Usage: bash install.sh <git-clone-url>"
    echo "  e.g. bash install.sh https://github.com/you/pi-display-server.git"
    echo "  Set DISPLAY_BACKEND=wayland for Sway/Wayland mode."
    exit 1
fi

# --- Dependencies ----------------------------------------------------------
echo "[1/6] Installing system dependencies…"
sudo apt-get update -qq

COMMON_DEPS=(git mpv chromium python3)

if [ "${BACKEND}" = "wayland" ]; then
    sudo apt-get install -y -qq "${COMMON_DEPS[@]}" \
        sway swaybg wlr-randr imv
else
    sudo apt-get install -y -qq "${COMMON_DEPS[@]}" \
        xserver-xorg xinit xinput openbox xdotool x11-utils feh
fi

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
sudo sed -i "s/^Environment=DISPLAY_BACKEND=.*/Environment=DISPLAY_BACKEND=${BACKEND}/" \
    "/etc/systemd/system/${SERVICE_NAME}.service" || true

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"

# --- Boot files (only if missing) ------------------------------------------
echo "[5/6] Setting up boot files…"

if [ "${BACKEND}" = "wayland" ]; then
    # --- Sway config ---
    SWAY_DIR="$HOME/.config/sway"
    mkdir -p "${SWAY_DIR}"
    if [ ! -f "${SWAY_DIR}/config" ]; then
        cat > "${SWAY_DIR}/config" << 'SWAYCONFIG'
# Pi Display Server — Sway kiosk config
output * bg #000000 solid_color
output HDMI-A-1 transform 90

input type:touch {
    map_to_output HDMI-A-1
}

# All windows float, no borders
default_border none
default_floating_border none
for_window [app_id=".*"] floating enable
for_window [app_id=".*"] border none
for_window [title=".*"] floating enable
for_window [title=".*"] border none

# Disable idle/screen blanking
exec swaymsg idle_inhibit visible

# Start display server
exec sudo systemctl restart pi-display-server
SWAYCONFIG
        echo "  Created ${SWAY_DIR}/config"
    else
        echo "  ${SWAY_DIR}/config already exists, skipping"
    fi

    if [ ! -f "$HOME/.bash_profile" ]; then
        cat > "$HOME/.bash_profile" << 'BASHPROFILE'
if [ -z "$WAYLAND_DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
  export XDG_RUNTIME_DIR="/run/user/$(id -u)"
  export XDG_SESSION_TYPE=wayland
  exec sway
fi
BASHPROFILE
        echo "  Created ~/.bash_profile (Wayland/Sway)"
    elif grep -q startx "$HOME/.bash_profile" 2>/dev/null && ! grep -q 'exec sway' "$HOME/.bash_profile" 2>/dev/null; then
        cp "$HOME/.bash_profile" "$HOME/.bash_profile.bak-x11"
        cat > "$HOME/.bash_profile" << 'BASHPROFILE'
if [ -z "$WAYLAND_DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
  export XDG_RUNTIME_DIR="/run/user/$(id -u)"
  export XDG_SESSION_TYPE=wayland
  exec sway
fi
BASHPROFILE
        echo "  Migrated ~/.bash_profile startx → sway (backup: ~/.bash_profile.bak-x11)"
    else
        echo "  ~/.bash_profile already exists, skipping"
    fi
else
    # --- X11 config ---
    if [ ! -f "$HOME/.xinitrc" ]; then
        cat > "$HOME/.xinitrc" << 'XINITRC'
xset -dpms
xset s off
xset s noblank
xrandr -o right

if command -v xinput >/dev/null 2>&1; then
  touch_ids=""
  for _ in $(seq 1 20); do
    touch_ids=$(xinput --list --short | awk '/slave[[:space:]]+pointer/ && tolower($0) ~ /(touch|stylus)/ {for (i = 1; i <= NF; i++) if ($i ~ /^id=/) {split($i, a, "="); print a[2]}}')
    [ -n "$touch_ids" ] && break
    sleep 1
  done
  for id in $touch_ids; do
    if xinput list-props "$id" | awk -F: '/Coordinate Transformation Matrix/ {found=1} END {exit !found}'; then
      xinput set-prop "$id" "Coordinate Transformation Matrix" 0 1 0 -1 0 1 0 0 1 || true
    fi
  done
fi

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
