#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/home/kaji_islam/smart-electricity"
BRANCH="feature/rolling-planner-simulator"
MARKER_BEGIN="# BEGIN SMART ELECTRICITY JOINT CONTROLLER"
MARKER_END="# END SMART ELECTRICITY JOINT CONTROLLER"

cd "$REPO_DIR"

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "STOP: tracked production files contain local changes."
  exit 1
fi

git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

export PYTHONPATH="$REPO_DIR/src"
.venv/bin/python -m unittest discover -s tests -v

mkdir -p logs data/foxess_schedule_backups backups
timestamp="$(date +%Y%m%d_%H%M%S)"
crontab -l > "backups/crontab-before-joint-${timestamp}.txt" 2>/dev/null || true
cp .env "backups/env-before-joint-${timestamp}" 
chmod 600 "backups/env-before-joint-${timestamp}"

# Build a fresh plan and prove that both phases can read the real inverter and
# construct a schedule without transmitting a write.
.venv/bin/python src/economic_plan_snapshot.py
.venv/bin/python src/foxess_joint_auto_execute.py --phase charge --dry-run --ignore-switches
.venv/bin/python src/foxess_joint_auto_execute.py --phase export --dry-run --ignore-switches
.venv/bin/python src/control_switches.py --all-on

if grep -q '^FOXESS_CONTROL_MODE=' .env; then
  sed -i.bak 's/^FOXESS_CONTROL_MODE=.*/FOXESS_CONTROL_MODE=joint/' .env
else
  printf '\nFOXESS_CONTROL_MODE=joint\n' >> .env
fi
chmod 600 .env

current_cron="$(mktemp)"
new_cron="$(mktemp)"
trap 'rm -f "$current_cron" "$new_cron"' EXIT
crontab -l > "$current_cron" 2>/dev/null || true

sed "/^${MARKER_BEGIN}$/,/^${MARKER_END}$/d" "$current_cron" > "$new_cron"
printf '\n%s\n' "$MARKER_BEGIN" >> "$new_cron"
cat deployment/foxess-joint-controller.cron >> "$new_cron"
printf '%s\n' "$MARKER_END" >> "$new_cron"
crontab "$new_cron"

sudo systemctl restart smart-electricity-dashboard.service
sudo systemctl is-active --quiet smart-electricity-dashboard.service

echo "Joint controller activated. The legacy executor will now skip writes."
echo "Crontab backup: backups/crontab-before-joint-${timestamp}.txt"
echo "Environment backup: backups/env-before-joint-${timestamp}"
