# Cloudflare Containers deployment guide

## Overview

NeoApp can run in a single Cloudflare Container with one Python container process and one supervised worker process.

The worker proxy in [cloudflare/src/index.js](cloudflare/src/index.js) starts the container, forwards the runtime environment, and routes requests into the Python app. The container runs both:

- the FastAPI backend
- the long-running trading worker

The deployment is intentionally conservative:

- `TRADING_ENABLED` defaults to `false`
- worker actions are not enabled unless you explicitly set the relevant environment variables and secrets
- the Cloudflare container is configured with `max_instances: 1`

## What must be present

### Cloudflare account and plan

You need:

- a Cloudflare account with access to Containers
- Wrangler v4 or later
- container registry access for the account

Check the current account capabilities before deploying. If Containers are not enabled, the config will not deploy.

### Required secrets

Set these with Wrangler secrets or the Cloudflare dashboard:

- `CONSUMER_KEY`
- `MOBILE`
- `UCC`
- `MPIN`
- `TOTP_SECRET`
- `WEB_API_TOKEN`

Optional but supported:

- `REMOTE_CONFIG_URL`
- `PROGRESSIVE_LOSS_URL`
- `TELEGRAM_CONFIGS`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

### Required environment variables

Safe defaults are already included in [wrangler.jsonc](../wrangler.jsonc), but you can override them if needed:

- `TRADING_ENABLED=false` for dry-run and dashboard-only validation
- `WEB_ALLOW_LOCAL_NOAUTH=false`
- `WEB_LOG_LEVEL=INFO`
- `WORKER_HEALTH_WINDOW_SECONDS=30`
- `WEB_POLL_SECONDS=5`
- `WEB_MAX_BACKOFF_SECONDS=60`

## Local Docker testing

Build the image:

```sh
docker build -t neoapp-cloudflare .
```

Run it with safe dry-run settings:

```sh
docker run --rm -p 8080:8080 \
  -e TRADING_ENABLED=false \
  -e WEB_API_TOKEN=demo-token \
  -e CONSUMER_KEY=... \
  -e MOBILE=... \
  -e UCC=... \
  -e MPIN=... \
  -e TOTP_SECRET=... \
  neoapp-cloudflare
```

The container should expose the FastAPI app on `http://localhost:8080`.

## Wrangler setup

From the repository root (the directory containing `package.json` and `package-lock.json`), install the Node tooling once:

```sh
cd /path/to/NeoApp2
npm ci
```

Authenticate Wrangler:

```sh
npx wrangler login
```

Check your container inventory:

```sh
npx wrangler containers list
```

Check images:

```sh
npx wrangler containers images list
```

## Deploy

Dry-run the config locally before production deploy:

```sh
cd /path/to/NeoApp2
npm ci
npx wrangler deploy --dry-run
```

Deploy the container and Worker:

```sh
cd /path/to/NeoApp2
npm ci
npx wrangler deploy
```

If the first deployment is slow, wait for the container to provision before retrying requests.

## Health checks

### Container health

```sh
curl -i http://localhost:8080/health
```

### Dashboard and API smoke tests

```sh
curl -i -H 'X-API-Key: demo-token' http://localhost:8080/api/system/status
curl -i -H 'X-API-Key: demo-token' http://localhost:8080/api/monitor/snapshot
```

### WebSocket smoke test

Use a WebSocket client against:

```text
ws://localhost:8080/ws/monitor?token=demo-token
```

Cloudflare will forward the WebSocket request through the Worker to the container.

## Rollback

1. Re-deploy the previous known-good Worker and container version.
2. Revert to the previous `wrangler.jsonc` and Dockerfile if the issue is config-related.
3. If you changed secrets, rotate them in Cloudflare immediately.

Use `wrangler containers instances` to inspect active container instances before and after rollback.

## Dry-run validation checklist

- `TRADING_ENABLED=false`
- auth token is present and tested with HTTP requests
- dashboard loads without placing orders
- `/health` responds successfully
- `/api/system/status` responds with `worker_snapshot_present` and auth metadata
- `/api/monitor/snapshot` returns warming-up or snapshot data
- `/ws/monitor` connects and receives updates
- no order placement endpoints are called in a live account
- `buy_disabled.json`, snapshot JSON, and trade CSV paths are treated as ephemeral unless you provide external persistence

## Known limitations and risks

### Long-running trading process

Cloudflare Containers are request-driven and can restart or sleep. NeoApp's worker is a perpetual loop, so container lifecycle changes can interrupt it.

### Container restarts

The worker restarts are best-effort only. A crash or deploy can interrupt the trading loop, WebSocket feed, and snapshot freshness.

### Local filesystem persistence

NeoApp still uses file-backed state:

- `logs/web_monitor_state.json`
- `buy_disabled.json`
- `logs/trades_web_YYYY-MM-DD.csv`
- `capital_history.csv`

These files are not guaranteed to persist across container replacement or rollback.

### WebSocket connections

WebSocket sessions can drop on restart, sleep, or deploy. Clients must reconnect.

### Kotak Neo API compatibility

Kotak Neo authentication still depends on the existing credentials, TOTP flow, and outbound network access. If the broker API changes, the container deployment does not change that dependency.

### Trading safety

Do not enable live trading until the dry-run checklist passes and you have verified the broker credentials, snapshot behavior, and WebSocket reconnect behavior in the container.