#!/usr/bin/env bash
set -euo pipefail

repo_dir="/home/kaji_islam/smart-electricity"
host_name="35-212-220-32.sslip.io"
marker_begin="# BEGIN WATTSIQ PUBLIC READ-ONLY"
marker_end="# END WATTSIQ PUBLIC READ-ONLY"

cd "$repo_dir"

site_link="$(sudo grep -rlF "server_name ${host_name};" /etc/nginx/sites-enabled | head -n 1)"
if [[ -z "$site_link" ]]; then
  echo "STOP: could not find the active Nginx site for ${host_name}."
  exit 1
fi

site_file="$(readlink -f "$site_link")"
case "$site_file" in
  /etc/nginx/sites-available/*|/etc/nginx/sites-enabled/*) ;;
  *)
    echo "STOP: refusing to edit unexpected Nginx path: $site_file"
    exit 1
    ;;
esac

timestamp="$(date +%Y%m%d_%H%M%S)"
backup_file="${repo_dir}/backups/nginx-before-wattsiq-${timestamp}.conf"
mkdir -p "${repo_dir}/backups"
sudo cp "$site_file" "$backup_file"
sudo chown "$(id -u):$(id -g)" "$backup_file"

temp_file="$(mktemp)"
trap 'rm -f "$temp_file"' EXIT

python3 - "$site_file" "$temp_file" "$marker_begin" "$marker_end" <<'PY'
from pathlib import Path
import sys

source_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])
marker_begin = sys.argv[3]
marker_end = sys.argv[4]
text = source_path.read_text(encoding="utf-8")

if marker_begin in text:
    before, remainder = text.split(marker_begin, 1)
    if marker_end not in remainder:
        raise SystemExit("STOP: WattsIQ Nginx marker is incomplete")
    _, after = remainder.split(marker_end, 1)
    text = before.rstrip() + "\n\n" + after.lstrip("\n")

anchor = "    location / {"
if anchor not in text:
    raise SystemExit("STOP: authenticated catch-all Nginx location was not found")

block = """    # BEGIN WATTSIQ PUBLIC READ-ONLY
    location = /wattsiq {
        auth_basic off;
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 10s;
        proxy_read_timeout 60s;
        add_header Cache-Control "no-store" always;
        add_header Referrer-Policy "no-referrer" always;
        add_header X-Content-Type-Options "nosniff" always;
    }

    location = /wattsiq/ {
        auth_basic off;
        return 308 /wattsiq;
    }

    location ^~ /wattsiq/api/ {
        auth_basic off;
        limit_except GET { deny all; }
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 10s;
        proxy_read_timeout 60s;
        add_header Cache-Control "no-store" always;
        add_header Referrer-Policy "no-referrer" always;
        add_header X-Content-Type-Options "nosniff" always;
    }
    # END WATTSIQ PUBLIC READ-ONLY

"""

output_path.write_text(text.replace(anchor, block + anchor, 1), encoding="utf-8")
PY

sudo install -o root -g root -m 0644 "$temp_file" "$site_file"
if ! sudo nginx -t; then
  sudo install -o root -g root -m 0644 "$backup_file" "$site_file"
  echo "STOP: Nginx validation failed; the previous configuration was restored."
  exit 1
fi

sudo systemctl restart smart-electricity-dashboard.service
sudo systemctl reload nginx

page="$(curl -fsS "https://${host_name}/wattsiq")"
grep -q '<title>WattsIQ</title>' <<<"$page"
if grep -q '>Main dashboard</a>' <<<"$page"; then
  echo "STOP: WattsIQ unexpectedly contains the operator dashboard link."
  exit 1
fi

curl -fsS -o /dev/null "https://${host_name}/wattsiq/api/optimizer-audit"
public_control_status="$(curl -sS -o /dev/null -w '%{http_code}' "https://${host_name}/wattsiq/api/control-switch")"
protected_root_status="$(curl -sS -o /dev/null -w '%{http_code}' "https://${host_name}/")"

if [[ "$public_control_status" != "404" ]]; then
  echo "STOP: unexpected public control route status: $public_control_status"
  exit 1
fi
if [[ "$protected_root_status" != "401" ]]; then
  echo "STOP: authenticated dashboard no longer returns 401 without credentials."
  exit 1
fi

echo "WattsIQ is live at https://${host_name}/wattsiq"
echo "Authenticated dashboard protection remains active."
echo "Nginx backup: $backup_file"
