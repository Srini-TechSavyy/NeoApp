#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <domain> <letsencrypt_email>"
  exit 1
fi

DOMAIN="$1"
LE_EMAIL="$2"
APP_USER="neoapp"
APP_DIR="/opt/neoapp2"
ENV_DIR="/etc/neoapp2"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

echo "[1/9] Installing OS packages"
sudo apt-get update
sudo apt-get install -y \
  python3 python3-venv python3-pip \
  git build-essential \
  nginx certbot python3-certbot-nginx curl

echo "[2/9] Creating app user and directories"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  sudo useradd -m -s /bin/bash "$APP_USER"
fi
sudo mkdir -p \
  "$APP_DIR"/releases \
  "$APP_DIR"/incoming \
  "$APP_DIR"/shared/logs \
  "$APP_DIR"/shared/data \
  "$APP_DIR"/shared/assets \
  "$ENV_DIR"
sudo chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

echo "[3/9] Creating venv"
if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
  sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
fi

echo "[4/9] Installing systemd unit files"
sudo cp "$REPO_ROOT/deploy/systemd/neo-fastapi.service" /etc/systemd/system/neo-fastapi.service
sudo cp "$REPO_ROOT/deploy/systemd/neo-worker.service" /etc/systemd/system/neo-worker.service

echo "[5/9] Installing Nginx HTTP site (TLS after certificate)"
sudo sed "s/__SERVER_NAME__/$DOMAIN/g" "$REPO_ROOT/deploy/nginx/neoapp2.http.conf" \
  | sudo tee /etc/nginx/sites-available/neoapp2 >/dev/null
sudo ln -sfn /etc/nginx/sites-available/neoapp2 /etc/nginx/sites-enabled/neoapp2
sudo rm -f /etc/nginx/sites-enabled/default /etc/nginx/sites-enabled/neoapp
sudo mkdir -p /var/www/html
sudo nginx -t
sudo systemctl reload nginx

echo "[6/9] Requesting TLS certificate (only if DNS points at this VM)"
PUBLIC_IP="$(curl -fsS --max-time 8 https://api.ipify.org || curl -fsS --max-time 8 https://ifconfig.me || true)"
RESOLVED_IPS="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ')"
echo "VM public IPv4: ${PUBLIC_IP:-unknown}"
echo "DNS A for $DOMAIN: ${RESOLVED_IPS:-none}"
if [[ -n "${PUBLIC_IP:-}" ]] && echo " ${RESOLVED_IPS} " | grep -q " ${PUBLIC_IP} "; then
  sudo certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$LE_EMAIL" --redirect
  sudo sed "s/__SERVER_NAME__/$DOMAIN/g" "$REPO_ROOT/deploy/nginx/neoapp2.conf" \
    | sudo tee /etc/nginx/sites-available/neoapp2 >/dev/null
  sudo nginx -t
  sudo systemctl reload nginx
else
  echo "Skipping Certbot: $DOMAIN does not resolve to this VM public IP."
  echo "Create an A record: $DOMAIN -> ${PUBLIC_IP:-<vm-public-ip>} (DNS only, not a CDN proxy)."
  echo "Then run: sudo certbot --nginx -d $DOMAIN --agree-tos -m $LE_EMAIL --redirect"
  echo "After a certificate exists: sudo sed 's/__SERVER_NAME__/$DOMAIN/g' $REPO_ROOT/deploy/nginx/neoapp2.conf | sudo tee /etc/nginx/sites-available/neoapp2"
  echo "Then: sudo nginx -t && sudo systemctl reload nginx"
fi

echo "[7/9] Runtime environment file template"
if [[ ! -f "$ENV_DIR/neoapp.env" ]]; then
  sudo install -o root -g "$APP_USER" -m 640 "$REPO_ROOT/deploy/env.example" "$ENV_DIR/neoapp.env"
  echo "Created $ENV_DIR/neoapp.env from deploy/env.example — edit secrets before starting services."
else
  echo "Keeping existing $ENV_DIR/neoapp.env"
fi

echo "[8/9] Enabling services"
sudo systemctl daemon-reload
sudo systemctl enable neo-fastapi.service neo-worker.service nginx

echo "[9/9] Done"
cat <<EOF

Next steps:
  1. Edit secrets (never commit this file):
       sudo nano $ENV_DIR/neoapp.env
     Generate WEB_API_TOKEN: openssl rand -hex 32

  2. Copy scrip master CSVs (not in git) to:
       $APP_DIR/shared/assets/nse_fo.csv
       $APP_DIR/shared/assets/bse_fo.csv

  3. Register a self-hosted GitHub Actions runner on this VM with labels:
       self-hosted, linux, x64, neoapp2

  4. Grant the runner user write access to $APP_DIR (replace RUNNER_USER):
       $REPO_ROOT/scripts/azure/grant_runner_deploy_access.sh RUNNER_USER
     Then restart the runner service.

  5. Allow the runner user passwordless systemctl (replace RUNNER_USER):
       sudo visudo -f /etc/sudoers.d/neoapp-deploy
     Example:
       RUNNER_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart neo-worker.service, /usr/bin/systemctl restart neo-fastapi.service, /usr/bin/systemctl reload nginx, /usr/bin/systemctl status neo-worker.service, /usr/bin/systemctl status neo-fastapi.service, /usr/sbin/nginx

  6. Deploy via GitHub Actions workflow "Deploy to Azure VM", then:
       sudo systemctl restart neo-worker.service neo-fastapi.service

Verification:
  curl -fsS http://127.0.0.1:8000/health
  curl -fsS https://$DOMAIN/health

See deploy/AZURE_DEPLOY.md for full runbook.
EOF
