#!/usr/bin/env bash
set -euo pipefail

PI_HOST="${PI_HOST:-pi@ap900-pi-kiosk.local}"
REMOTE_DIR="/opt/pi-display-server"
REPO_URL="$(git remote get-url origin)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:-update}"

if [ "${MODE}" != "update" ] && [ "${MODE}" != "--bootstrap" ]; then
  echo "Uso: ./deploy.sh [--bootstrap]"
  exit 1
fi

echo "Pushing to git…"
git push

if [ "${MODE}" = "--bootstrap" ]; then
  echo "Bootstrapping Pi (${PI_HOST}) with install.sh…"
  scp "${SCRIPT_DIR}/install.sh" "${PI_HOST}:/tmp/install-pi-display-server.sh"
  ssh "${PI_HOST}" "set -e
    chmod +x /tmp/install-pi-display-server.sh
    bash /tmp/install-pi-display-server.sh ${REPO_URL}
    rm -f /tmp/install-pi-display-server.sh
  "
  echo "Done — bootstrap finished"
  exit 0
fi

echo "Updating Pi (${PI_HOST})…"
ssh "${PI_HOST}" "set -e
  if [ ! -d ${REMOTE_DIR}/.git ]; then
    echo 'First deploy: converting to git-managed install…'
    if ! command -v git >/dev/null 2>&1; then
      echo 'Installing git…'
      sudo apt-get update -qq
      sudo apt-get install -y -qq git
    fi
    if ! command -v xdpyinfo >/dev/null 2>&1; then
      echo 'Installing x11-utils…'
      sudo apt-get update -qq
      sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq x11-utils
    fi
    sudo rm -rf ${REMOTE_DIR}
    sudo git clone ${REPO_URL} ${REMOTE_DIR}
    sudo chmod +x ${REMOTE_DIR}/update.sh
    sudo ln -sf ${REMOTE_DIR}/update.sh /usr/local/bin/update-display
    sudo cp ${REMOTE_DIR}/pi-display-server.service /etc/systemd/system/pi-display-server.service
    sudo systemctl daemon-reload
    sudo systemctl restart pi-display-server
    echo 'Done — migrated to git workflow'
  else
    update-display
  fi
"
