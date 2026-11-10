#!/bin/bash
# Runs ON the Azure VM (Ubuntu 24.04). Called by deploy_azure.bat; safe to run again for updates.
#   sudo bash /tmp/setup.sh <public-dns-name>
# Installs Python + Caddy (automatic HTTPS, needed for the phone microphone), unpacks the app to /opt/saarthi,
# and runs it as a service that restarts on crash or reboot.
# Mode: "full" if .env has GNANI_API_KEY and LLM_API_KEY, otherwise "review" (synthetic demo, no keys needed).
set -euo pipefail
HOST="${1:?usage: setup.sh <public-dns-name>}"
APP=/opt/saarthi
export DEBIAN_FRONTEND=noninteractive

echo "== packages"
apt-get update -y -q
apt-get install -y -q python3-venv python3-pip curl gpg debian-keyring debian-archive-keyring apt-transport-https libgl1 >/dev/null
if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y -q && apt-get install -y -q caddy >/dev/null
fi

echo "== swap (small VMs have 1 GB RAM)"
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== app"
id saarthi >/dev/null 2>&1 || useradd -r -m -d "$APP" -s /usr/sbin/nologin saarthi
systemctl stop saarthi 2>/dev/null || true
mkdir -p "$APP/data"
tar -xzf /tmp/saarthi.tgz -C "$APP"      # keeps $APP/data (database) between updates
chown -R saarthi:saarthi "$APP"
[ -x "$APP/.venv/bin/python" ] || sudo -u saarthi python3 -m venv "$APP/.venv"
sudo -u saarthi "$APP/.venv/bin/pip" install -q --upgrade pip
sudo -u saarthi "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

MODE=review
if [ -f "$APP/.env" ] && grep -qE '^GNANI_API_KEY=[A-Za-z0-9]' "$APP/.env" && ! grep -qE '^GNANI_API_KEY=your_' "$APP/.env" \
   && grep -qE '^LLM_API_KEY=[A-Za-z0-9]' "$APP/.env" && ! grep -qE '^LLM_API_KEY=your_' "$APP/.env"; then
  MODE=full
fi
KEYFILE="$APP/data/review_key"
[ -s "$KEYFILE" ] || openssl rand -hex 6 > "$KEYFILE"
REVIEW_KEY=$(cat "$KEYFILE")
chown saarthi:saarthi "$KEYFILE"

cat > /etc/systemd/system/saarthi.service <<EOF
[Unit]
Description=Saarthi fleet agents ($MODE mode)
After=network-online.target
Wants=network-online.target

[Service]
User=saarthi
WorkingDirectory=$APP
Environment=PATH=$APP/.venv/bin:/usr/local/bin:/usr/bin:/bin
Environment=SAARTHI_MODE=$MODE
Environment=PORT=8080
Environment=PUBLIC_BASE_URL=https://$HOST
Environment=REVIEW_KEY=$REVIEW_KEY
ExecStart=/bin/sh $APP/scripts/docker_start.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/caddy/Caddyfile <<EOF
$HOST {
  encode gzip
  reverse_proxy 127.0.0.1:8080
}
EOF

systemctl daemon-reload
systemctl enable --now saarthi >/dev/null
systemctl restart caddy

echo "== waiting for the app"
for i in $(seq 1 60); do
  curl -sf http://127.0.0.1:8080/health >/dev/null && break
  sleep 3
done
curl -sf http://127.0.0.1:8080/health >/dev/null || { echo "App did not start. Last log lines:"; journalctl -u saarthi -n 40 --no-pager; exit 1; }

echo
echo "=================================================================="
echo " Saarthi is LIVE ($MODE mode)"
if [ "$MODE" = review ]; then
  echo " Review link (CEO office + driver side):  https://$HOST/review?k=$REVIEW_KEY"
else
  OFFICE_KEY=$(grep -E '^API_SHARED_SECRET=' "$APP/.env" | cut -d= -f2-)
  echo " Office (CEO Agent):  https://$HOST/office      office key: $OFFICE_KEY"
  echo " Driver links:"
  sudo -u saarthi "$APP/.venv/bin/python" - <<PY
import sqlite3
con = sqlite3.connect("$APP/data/saarthi.db")
for code, reg, tok in con.execute("SELECT code, reg_label, access_token FROM vehicles ORDER BY code"):
    print(f"   {reg}: https://$HOST/driver?v={code}&k={tok}")
PY
fi
echo " (HTTPS certificate can take 1-2 minutes on the very first start)"
echo "=================================================================="
echo "LINK https://$HOST/review?k=$REVIEW_KEY" > "$APP/data/last_deploy.txt"
