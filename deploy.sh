#!/usr/bin/env bash
set -euo pipefail

PI_HOST="${PI_HOST:-pi@ap900-pi-kiosk.local}"
REMOTE_DIR="/opt/pi-display-server"
REPO_URL="$(git remote get-url origin)"

echo "Pushing to git…"
git push

echo "Updating Pi (${PI_HOST})…"
ssh "${PI_HOST}" "
  if [ ! -d ${REMOTE_DIR}/.git ]; then
    echo 'First deploy: converting to git-managed install…'
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
