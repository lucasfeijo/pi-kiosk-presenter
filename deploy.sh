#!/usr/bin/env bash
set -euo pipefail

PI_HOST="${PI_HOST:-pi@ap900-pi-kiosk.local}"

echo "Pushing to git…"
git push

echo "Updating Pi (${PI_HOST})…"
ssh "${PI_HOST}" "update-display"
