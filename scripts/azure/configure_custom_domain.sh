#!/usr/bin/env bash
# Add an HTTP Nginx vhost for a custom domain on an existing NeoApp2 VM.
# Does not change systemd units, neoapp.env, or application code.
# Does not request a Let's Encrypt certificate unless DNS A records include this VM's public IP.
set -euo pipefail

DOMAIN="${1:-neo.techsavyy.com}"
LE_EMAIL="${2:-}"
SITE_NAME="${DOMAIN}"
SITE="/etc/nginx/sites-available/${SITE_NAME}"
BACKUP_DIR="/etc/nginx/sites-available/backups"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HTTP_TMPL="$REPO_ROOT/deploy/nginx/neoapp2.http.conf"
TLS_TMPL="$REPO_ROOT/deploy/nginx/neoapp2.conf"

if [[ ! -f "$HTTP_TMPL" ]]; then
  echo "Missing template: $HTTP_TMPL"
  exit 1
fi

echo "Domain: $DOMAIN"
echo "[1/5] Backing up existing site file if present"
sudo mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d%H%M%S)"
if [[ -f "$SITE" ]]; then
  sudo cp -a "$SITE" "$BACKUP_DIR/${SITE_NAME}.${STAMP}.bak"
  echo "Backup: $BACKUP_DIR/${SITE_NAME}.${STAMP}.bak"
fi

echo "[2/5] Installing HTTP vhost $SITE (existing neoapp2/sslip.io site is left unchanged)"
sudo sed "s/__SERVER_NAME__/$DOMAIN/g" "$HTTP_TMPL" | sudo tee "$SITE" >/dev/null
sudo ln -sfn "$SITE" "/etc/nginx/sites-enabled/${SITE_NAME}"
sudo rm -f /etc/nginx/sites-enabled/default
sudo mkdir -p /var/www/html

echo "[3/5] nginx -t"
sudo nginx -t
echo "[4/5] Reloading Nginx (not FastAPI/worker)"
sudo systemctl reload nginx

PUBLIC_IP="$(curl -fsS --max-time 8 https://api.ipify.org || curl -fsS --max-time 8 https://ifconfig.me || true)"
RESOLVED_IPS="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ')"
echo "VM public IPv4: ${PUBLIC_IP:-unknown}"
echo "DNS A for $DOMAIN: ${RESOLVED_IPS:-none}"

echo "[5/5] Certbot gate"
if [[ -z "${PUBLIC_IP:-}" ]] || ! echo " ${RESOLVED_IPS} " | grep -q " ${PUBLIC_IP} "; then
  echo "Not requesting a certificate: DNS for $DOMAIN does not resolve to ${PUBLIC_IP:-this VM}."
  echo "Required A record: $DOMAIN -> ${PUBLIC_IP:-<vm-public-ip>}"
  echo "If the name is on Cloudflare, set the record to DNS only (grey cloud) so the public A record is the VM IP."
  echo "After DNS is correct, rerun:"
  echo "  $0 $DOMAIN <letsencrypt-email>"
  echo "Or:"
  echo "  sudo certbot --nginx -d $DOMAIN --agree-tos -m <letsencrypt-email> --redirect"
  echo "  sudo sed 's/__SERVER_NAME__/$DOMAIN/g' $TLS_TMPL | sudo tee $SITE"
  echo "  sudo nginx -t && sudo systemctl reload nginx"
  exit 0
fi

if [[ -z "$LE_EMAIL" ]]; then
  echo "DNS matches this VM, but Let's Encrypt email was not provided."
  echo "Not guessing an email. Rerun: $0 $DOMAIN <letsencrypt-email>"
  exit 2
fi

sudo certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$LE_EMAIL" --redirect
sudo sed "s/__SERVER_NAME__/$DOMAIN/g" "$TLS_TMPL" | sudo tee "$SITE" >/dev/null
sudo nginx -t
sudo systemctl reload nginx
echo "Certificate installed for $DOMAIN"
