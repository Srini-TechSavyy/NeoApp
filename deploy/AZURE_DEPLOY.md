# Azure VM Deployment (API + Worker + Nginx + TLS)

NeoApp web stack (FastAPI + worker + static UI) runs on an Ubuntu Azure VM at `/opt/neoapp2`. Deployment uses a **self-hosted GitHub Actions runner** on the VM—not SSH from GitHub-hosted runners.

Cloudflare deployment remains separate; see [docs/CLOUDFLARE_DEPLOYMENT.md](../docs/CLOUDFLARE_DEPLOYMENT.md).

## Architecture

```
/opt/neoapp2/
├── venv/                    # Python virtualenv (shared across releases)
├── releases/<timestamp-id>/ # Versioned deploy bundles
├── current -> releases/...  # Active release symlink
└── shared/
    ├── logs/                # Snapshot, lock, trade journal CSVs
    ├── data/                # buy_disabled.json (risk controls)
    └── assets/              # nse_fo.csv, bse_fo.csv (not in git)

/etc/neoapp2/neoapp.env       # Secrets and path overrides (640, root:neoapp)
```

Systemd: `neo-fastapi.service` (127.0.0.1:8000), `neo-worker.service`. Nginx terminates TLS and proxies HTTP + WebSocket (`/ws/monitor`).

---

## 1) Azure VM preparation

1. Ubuntu 22.04+ VM with public IP or DNS name.
2. Network security group: allow **22**, **80**, **443** inbound. Do **not** expose port **8000** publicly.
3. SSH in as a sudo-capable user.

---

## 2) Initial bootstrap (once)

Clone the repo on the VM (or copy deploy files), then:

```bash
cd NeoApp   # repository root
chmod +x scripts/azure/setup_vm.sh
./scripts/azure/setup_vm.sh <your-domain> <letsencrypt-email>
```

This script:

- Installs `python3-venv`, **git**, **build-essential**, `nginx`, `certbot`
- Creates user `neoapp`, dirs under `/opt/neoapp2` including `shared/{logs,data,assets}`
- Creates `/opt/neoapp2/venv`
- Installs systemd units from [deploy/systemd/](systemd/)
- Configures Nginx + Let's Encrypt from [deploy/nginx/neoapp2.conf](nginx/neoapp2.conf)
- Seeds `/etc/neoapp2/neoapp.env` from [deploy/env.example](env.example) if missing

---

## 3) Environment variables and secrets

Edit on the VM only:

```bash
openssl rand -hex 32   # use for WEB_API_TOKEN
sudo nano /etc/neoapp2/neoapp.env
sudo chown root:neoapp /etc/neoapp2/neoapp.env
sudo chmod 640 /etc/neoapp2/neoapp.env
```

Use [deploy/env.example](env.example) as the reference. **Production defaults:**

- `TRADING_ENABLED=false`
- `WEB_ALLOW_LOCAL_NOAUTH=false`

Copy scrip master files (not committed to git):

```bash
sudo cp nse_fo.csv /opt/neoapp2/shared/assets/
sudo cp bse_fo.csv /opt/neoapp2/shared/assets/
sudo chown neoapp:neoapp /opt/neoapp2/shared/assets/*.csv
```

See also [docs/VM_SECRET_UPDATE.md](../docs/VM_SECRET_UPDATE.md).

### Enable live trading (manual, after validation)

Only after completing the validation checklist below, set `TRADING_ENABLED=true` in `neoapp.env` and restart services. Do not enable during initial deploy.

---

## 4) Self-hosted GitHub Actions runner

1. In GitHub: **Settings → Actions → Runners → New self-hosted runner** (Linux x64).
2. Install and start the runner on the **same VM**.
3. Add labels: `self-hosted`, `linux`, `x64`, `neoapp2` (must match [.github/workflows/deploy-azure-vm.yml](../.github/workflows/deploy-azure-vm.yml)).

### Passwordless sudo for deploy

The workflow runs `sudo -n systemctl` and `nginx -t`. Grant the **runner user** (not necessarily `neoapp`):

```bash
sudo visudo -f /etc/sudoers.d/neoapp-deploy
```

```
RUNNER_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart neo-worker.service, /usr/bin/systemctl restart neo-fastapi.service, /usr/bin/systemctl reload nginx, /usr/bin/systemctl status neo-worker.service, /usr/bin/systemctl status neo-fastapi.service, /usr/bin/nginx
```

Replace `RUNNER_USER` with `whoami` on the runner account.

### GitHub environment

Workflow uses GitHub Environment **`production`**. No `AZURE_VM_*` secrets are required for the self-hosted design.

Optional repository variable/secret for future external checks: `AZURE_PUBLIC_URL` (e.g. `https://your-domain`).

---

## 5) Systemd installation and service control

Bootstrap copies units to `/etc/systemd/system/`. After editing units in the repo on a running VM:

```bash
sudo cp deploy/systemd/neo-fastapi.service /etc/systemd/system/
sudo cp deploy/systemd/neo-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable neo-worker.service neo-fastapi.service nginx
sudo systemctl restart neo-worker.service neo-fastapi.service
sudo systemctl status neo-worker.service --no-pager
sudo systemctl status neo-fastapi.service --no-pager
```

Logs via journald:

```bash
journalctl -u neo-fastapi -u neo-worker -f
journalctl -u neo-fastapi -n 100 --no-pager
```

---

## 6) Nginx and HTTPS

Site file: [deploy/nginx/neoapp2.conf](nginx/neoapp2.conf). After manual edits:

```bash
sudo nginx -t
sudo systemctl reload nginx
sudo certbot certificates
```

Certbot renewal is typically via systemd timer (`certbot renew`).

---

## 7) Deploy application release

1. GitHub → **Actions** → **Deploy to Azure VM** → **Run workflow**
2. Set **deploy_ref** (default `dev`) to the branch/tag to deploy.
3. Workflow on the self-hosted runner:
   - Rsyncs code to `/opt/neoapp2/releases/<id>`
   - Symlinks `logs` → `shared/logs`
   - `pip install` into `/opt/neoapp2/venv`
   - Switches `/opt/neoapp2/current`
   - Restarts worker + API, reloads Nginx
   - Hits `/health` (and `/api/system/status` if token configured)
   - **Rolls back** to previous release if health check fails

First deploy: ensure `/opt/neoapp2/current` exists or workflow creates first release; no rollback target until a second successful deploy.

---

## 8) Health checks and smoke tests

**Local on VM:**

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/api/system/status -H "X-API-Key: <WEB_API_TOKEN>"
curl -fsS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/api/monitor/snapshot
# Expect 401 without header when token is configured
```

**Via Nginx:**

```bash
curl -fsS https://<your-domain>/health
curl -fsS https://<your-domain>/ -o /dev/null -w "%{http_code}\n"
```

**WebSocket** (replace token):

```bash
# wscat -c "wss://<your-domain>/ws/monitor?token=<WEB_API_TOKEN>"
```

**Worker snapshot file:**

```bash
ls -la /opt/neoapp2/shared/logs/web_monitor_state.json
```

---

## 9) Validation checklist (`TRADING_ENABLED=false`)

- [ ] Python 3.11+ in venv; `pip install -r requirements-web.txt` succeeds
- [ ] `neo-fastapi` and `neo-worker` active (`systemctl is-active`)
- [ ] `/health` OK on 127.0.0.1 and HTTPS
- [ ] Frontend loads at `/`
- [ ] API returns 401 without `X-API-Key`; 200 with valid key
- [ ] WebSocket monitor connects with `?token=`
- [ ] Worker updates snapshot timestamp within `WORKER_HEALTH_WINDOW_SECONDS`
- [ ] Scrip CSVs present under `shared/assets/`
- [ ] Reboot test: services start automatically
- [ ] No secrets committed to git

---

## 10) Rollback

**Automatic:** failed health check after deploy reverts `current` symlink to the previous release and restarts services.

**Manual:**

```bash
PREV=/opt/neoapp2/releases/<previous-release-id>
sudo ln -sfn "$PREV" /opt/neoapp2/current
sudo ln -sfn /opt/neoapp2/shared/logs /opt/neoapp2/current/logs
sudo systemctl restart neo-worker.service neo-fastapi.service
curl -fsS http://127.0.0.1:8000/health
```

---

## 11) Troubleshooting

| Symptom | Likely cause |
|--------|----------------|
| Health check fails after deploy | Check `journalctl -u neo-fastapi`; rollback may have run |
| 503 on API routes | `WEB_API_TOKEN` empty and `WEB_ALLOW_LOCAL_NOAUTH=false` |
| Worker stale / empty snapshot | Neo login failure; check credentials in `neoapp.env` and worker logs |
| Symbol not found | Missing or outdated CSV in `shared/assets/` |
| pip install fails on deploy | Install `git` and `build-essential`; network to GitHub for Neo API package |
| sudo password prompt in Actions | Fix `/etc/sudoers.d/neoapp-deploy` for runner user |
| WebSocket drops | Confirm Nginx `/ws/monitor` block; `proxy_buffering off` |

---

## 12) Deployment verification status

Record results here after running bootstrap + workflow on **your** Azure VM. Local repo changes alone do not constitute a completed production deploy.

| Check | Result | Date | Notes |
|-------|--------|------|-------|
| VM bootstrap | Pending operator | | Run `setup_vm.sh` |
| GitHub workflow deploy | Pending operator | | Self-hosted runner required |
| `/health` on VM | Pending operator | | |
| HTTPS + auth + WS | Pending operator | | |

---

## Source files

- Workflow: [.github/workflows/deploy-azure-vm.yml](../.github/workflows/deploy-azure-vm.yml)
- Bootstrap: [scripts/azure/setup_vm.sh](../scripts/azure/setup_vm.sh)
- Env template: [deploy/env.example](env.example)
- Nginx: [deploy/nginx/neoapp2.conf](nginx/neoapp2.conf)
- Systemd: [deploy/systemd/neo-fastapi.service](systemd/neo-fastapi.service), [deploy/systemd/neo-worker.service](systemd/neo-worker.service)
