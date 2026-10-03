#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/home/kaji_islam/smart-electricity"
MARKER_BEGIN="# BEGIN SMART ELECTRICITY JOINT CONTROLLER"
MARKER_END="# END SMART ELECTRICITY JOINT CONTROLLER"

cd "$REPO_DIR"
export PYTHONPATH="$REPO_DIR/src"

# Clear controller-owned schedule periods while joint writes are still allowed.
.venv/bin/python src/control_switches.py --all-off
FOXESS_CONTROL_MODE=joint .venv/bin/python src/foxess_joint_auto_execute.py --phase watchdog

if grep -q '^FOXESS_CONTROL_MODE=' .env; then
  sed -i.bak 's/^FOXESS_CONTROL_MODE=.*/FOXESS_CONTROL_MODE=rule/' .env
else
  printf '\nFOXESS_CONTROL_MODE=rule\n' >> .env
fi
chmod 600 .env

current_cron="$(mktemp)"
new_cron="$(mktemp)"
trap 'rm -f "$current_cron" "$new_cron"' EXIT
crontab -l > "$current_cron" 2>/dev/null || true
sed "/^${MARKER_BEGIN}$/,/^${MARKER_END}$/d" "$current_cron" > "$new_cron"
crontab "$new_cron"

sudo systemctl restart smart-electricity-dashboard.service
sudo systemctl is-active --quiet smart-electricity-dashboard.service

echo "Joint controller removed; legacy rule controller owns writes again."
