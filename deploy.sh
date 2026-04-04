#!/usr/bin/env bash
set -euo pipefail

PI_HOST="${PI_HOST:-pi@ap900-pi-kiosk.local}"
REMOTE_DIR="/opt/pi-display-server"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Deploying…"
scp -q "${SCRIPT_DIR}/display_server.py" "${SCRIPT_DIR}/pi-display-server.service" "${PI_HOST}:/tmp/"
ssh "${PI_HOST}" "\
  sudo mv /tmp/display_server.py ${REMOTE_DIR}/display_server.py && \
  sudo chmod +r ${REMOTE_DIR}/display_server.py && \
  sudo mv /tmp/pi-display-server.service /etc/systemd/system/pi-display-server.service && \
  sudo systemctl daemon-reload && \
  sudo systemctl restart pi-display-server && \
  echo 'Restarted OK'"
