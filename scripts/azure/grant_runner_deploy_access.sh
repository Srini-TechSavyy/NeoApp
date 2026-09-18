#!/usr/bin/env bash
# Grant the GitHub Actions runner user write access under /opt/neoapp2.
# Run on the Azure VM as a sudo-capable user (not inside GitHub Actions).
set -euo pipefail

APP_USER="neoapp"
APP_DIR="/opt/neoapp2"
RUNNER_USER="${1:-}"

if [[ -z "$RUNNER_USER" ]]; then
  RUNNER_USER="$(ps -o user= -C Runner.Listener 2>/dev/null | awk '{print $1}' | head -1 || true)"
fi

if [[ -z "$RUNNER_USER" ]]; then
  echo "Usage: $0 <github-actions-runner-username>"
  echo "Could not auto-detect Runner.Listener. Pass the user that owns the runner process."
  exit 1
fi

if ! id -u "$RUNNER_USER" >/dev/null 2>&1; then
  echo "User does not exist: $RUNNER_USER"
  exit 1
fi

if ! id -u "$APP_USER" >/dev/null 2>&1; then
  echo "App user does not exist: $APP_USER (run setup_vm.sh first)"
  exit 1
fi

echo "Runner user: $RUNNER_USER ($(id -u "$RUNNER_USER"))"
echo "Adding $RUNNER_USER to group $APP_USER"
sudo usermod -aG "$APP_USER" "$RUNNER_USER"

echo "Fixing ownership and group-write on $APP_DIR"
sudo mkdir -p "$APP_DIR/releases" "$APP_DIR/shared/logs" "$APP_DIR/shared/data" "$APP_DIR/shared/assets"
sudo chown -R "$APP_USER:$APP_USER" "$APP_DIR"
sudo chmod -R g+rwX "$APP_DIR"
sudo find "$APP_DIR" -type d -exec chmod 2775 {} \;

# ACL takes effect immediately (group membership does not apply until the runner restarts).
if command -v setfacl >/dev/null 2>&1 || sudo apt-get install -y acl; then
  echo "Setting POSIX ACL for $RUNNER_USER on $APP_DIR"
  sudo setfacl -R -m "u:${RUNNER_USER}:rwx" "$APP_DIR"
  sudo setfacl -R -d -m "u:${RUNNER_USER}:rwx" "$APP_DIR"
fi

echo
echo "Verify (no runner restart required for ACL):"
sudo -u "$RUNNER_USER" mkdir -p "$APP_DIR/releases/_permcheck"
sudo rmdir "$APP_DIR/releases/_permcheck"
echo "Write check: OK"

echo
echo "Still restart the runner so group membership applies for other tools:"
echo "  sudo systemctl restart 'actions.runner.*'"
echo "  # or: cd <actions-runner-dir> && sudo ./svc.sh restart"
echo
echo "Then re-run the Deploy to Azure VM workflow."
