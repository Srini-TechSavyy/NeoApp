import { Container, getContainer } from "@cloudflare/containers";

const CONTAINER_NAME = "neoapp";

const FORWARDED_ENV_KEYS = [
  "CONSUMER_KEY",
  "MOBILE",
  "UCC",
  "MPIN",
  "TOTP_SECRET",
  "TRADING_ENABLED",
  "WEB_API_TOKEN",
  "WEB_ALLOW_LOCAL_NOAUTH",
  "WEB_LOG_LEVEL",
  "WORKER_HEALTH_WINDOW_SECONDS",
  "WEB_POLL_SECONDS",
  "WEB_MAX_BACKOFF_SECONDS",
  "WEB_STATE_FILE",
  "WEB_STATE_LOCK_FILE",
  "BUY_DISABLED_FILE",
  "AUTO_BUY_ENABLED",
  "AUTO_SELL_ENABLED",
  "AUTO_LOTS",
  "AUTO_TARGET",
  "AUTO_SL",
  "AUTO_TSL_STEP",
  "AUTO_PT_STEP",
  "AUTO_BASE_INDEX",
  "AUTO_STRIKE_OFFSET",
  "NIFTY_LOT_SIZE",
  "REMOTE_CONFIG_URL",
  "PROGRESSIVE_LOSS_URL",
  "TELEGRAM_CONFIGS",
  "TELEGRAM_BOT_TOKEN",
  "TELEGRAM_CHAT_ID",
  "INITIAL_CAPITAL",
  "CAPITAL_TOPUP",
  "PNL_RESET_DATE",
  "MAX_DAILY_LOSS_COUNT"
];

function buildContainerEnv(env) {
  const containerEnv = {};

  for (const key of FORWARDED_ENV_KEYS) {
    const value = env[key];
    if (typeof value === "string" && value.length > 0) {
      containerEnv[key] = value;
    }
  }

  return containerEnv;
}

export class NeoAppContainer extends Container {
  defaultPort = 8080;
  sleepAfter = "24h";
}

async function startContainer(env) {
  const container = getContainer(env.NEOAPP, CONTAINER_NAME);
  const envVars = buildContainerEnv(env);

  await container.startAndWaitForPorts({
    startOptions: {
      envVars,
    },
  });

  return container;
}

export default {
  async fetch(request, env) {
    const container = await startContainer(env);
    return container.fetch(request);
  },
};