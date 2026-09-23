# config.py
import os
import json
from dotenv import load_dotenv
from common.utils import fetch_remote_config, fetch_remote_json, get_resource_path

load_dotenv(get_resource_path(".env"))

CONSUMER_KEY = os.getenv("CONSUMER_KEY")
MOBILE = os.getenv("MOBILE")
UCC = os.getenv("UCC")
TOTP_SECRET = os.getenv("TOTP_SECRET")
MPIN = os.getenv("MPIN")
LOT_SIZE = int(os.getenv("NIFTY_LOT_SIZE", "75"))

# Scrip Master constants
NSE_SCRIP_MASTER_PATH = get_resource_path(os.getenv("NSE_SCRIP_MASTER_PATH", "nse_fo.csv"))
BSE_SCRIP_MASTER_PATH = get_resource_path(os.getenv("BSE_SCRIP_MASTER_PATH", "bse_fo.csv"))
DEFAULT_TRADING_SYMBOL = "NIFTY"

from datetime import datetime, timedelta

def _format_neo_expiry(expiry: datetime) -> str:
    """
    Kotak trading symbols:
    - Monthly expiry (last expiry weekday of the month): YYMMM, e.g. 26SEP.
      The weekly code does not exist for that date.
    - Other weeklies: YYMDD with month not zero-padded, e.g. 26917 for 2026-09-17.
    """
    next_week = expiry + timedelta(days=7)
    if next_week.month != expiry.month:
        return expiry.strftime("%y%b").upper()
    return f"{expiry:%y}{expiry.month}{expiry:%d}"


def get_next_expiry(symbol: str) -> str:
    """
    Returns the nearest expiry token for the given symbol.
    NIFTY: Tuesday expiry, SENSEX: Thursday expiry.
    If today is expiry day and before 3:30pm, use today. Otherwise, use next week.
    """
    now = datetime.now()
    weekday = now.weekday()  # Monday=0, Sunday=6
    symbol_upper = symbol.upper()
    if symbol_upper == "NIFTY":
        expiry_weekday = 1  # Tuesday
    elif symbol_upper == "SENSEX":
        expiry_weekday = 3  # Thursday
    else:
        expiry_weekday = 1  # Default to Tuesday

    # Calculate days until expiry weekday
    days_ahead = (expiry_weekday - weekday) % 7
    expiry = now + timedelta(days=days_ahead)
    expiry_time = expiry.replace(hour=15, minute=30, second=0, microsecond=0)

    # If today is expiry day and before expiry time, use today
    if weekday == expiry_weekday and now < expiry_time:
        expiry = now
    # If today is after expiry time, move to next week's expiry
    elif weekday == expiry_weekday and now >= expiry_time:
        expiry = now + timedelta(days=7)
    # Otherwise, expiry is this week's upcoming expiry weekday
    expiry_str = _format_neo_expiry(expiry)
    print(f"[DEBUG] get_next_expiry called: now={now}, symbol={symbol}, expiry_str={expiry_str}")
    return expiry_str


# Buy disable feature
BUY_DISABLE_LOSS_COUNT = 3  # Number of continuous losses to disable buying
BUY_DISABLE_DURATION = 120  # seconds
ENABLE_BUY_DISABLE = True   # Enable/Disable the lockout feature

# Progressive Loss Lockout (Loss amount, Duration in minutes)
# Once a threshold is hit, buying is disabled for the specified duration.
PROGRESSIVE_LOSS_CONFIG_DEFAULT = [
    (2501, 1440), # >2500 loss -> Hard stop (24 hours)
    (2500, 30),   # 2500 loss -> 30 minutes
    (2000, 20),   # 2000 loss -> 20 minutes
    (1500, 15),   # 1500 loss -> 15 minutes
    (1000, 10),   # 1000 loss -> 10 minutes
    (500, 5),     # 500 loss -> 5 minutes
]

# Remote config for Maximum Loss (Higher friction to cheat)
# Replace these URLs with your own private GitHub Gist (Raw) URLs
REMOTE_CONFIG_URL = os.getenv("REMOTE_CONFIG_URL", "https://gist.githubusercontent.com/SriniArch/a81d5c68cdb91a432225b26833bef01d/raw/gistfile1.txt")
PROGRESSIVE_LOSS_URL = os.getenv("PROGRESSIVE_LOSS_URL", "https://gist.githubusercontent.com/SriniArch/0a485812216952d95e0dfc02f5e8a018/raw/progressive_loss.json")

PROGRESSIVE_LOSS_CONFIG = fetch_remote_json(PROGRESSIVE_LOSS_URL, PROGRESSIVE_LOSS_CONFIG_DEFAULT)

BUY_DISABLE_MAX_LOSS = 2501 # Final hard stop if not covered by progressive config
BUY_DISABLE_MAX_PROFIT = 2500 # Disable buying if net profit exceeds this amount
COOL_OFF_PERIOD = int(os.getenv("COOL_OFF_PERIOD", "0"))  # seconds; minimum gap between trades (0 = disabled)
TELEGRAM_COOL_OFF = int(os.getenv("TELEGRAM_COOL_OFF", "60"))  # seconds between telegram alerts

# Telegram recipients: prefer JSON list in TELEGRAM_CONFIGS, fallback to single token/chat env vars.
_telegram_configs_raw = os.getenv("TELEGRAM_CONFIGS", "").strip()
TELEGRAM_CONFIGS = []
if _telegram_configs_raw:
    try:
        parsed = json.loads(_telegram_configs_raw)
        if isinstance(parsed, dict):
            parsed = [parsed]
        if isinstance(parsed, list):
            TELEGRAM_CONFIGS = [c for c in parsed if isinstance(c, dict)]
    except Exception:
        TELEGRAM_CONFIGS = []

if not TELEGRAM_CONFIGS:
    _bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    _chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if _bot_token and _chat_id:
        TELEGRAM_CONFIGS = [{"token": _bot_token, "chat_id": _chat_id}]

# Per-trade default risk management
NIFTY_CONFIG = {
    "target": 2,
    "sl": 1.5,
    "tsl": 1,
    "pt": 1
}

SENSEX_CONFIG = {
    "target": 5,
    "sl": 4,
    "tsl": 2,
    "pt": 2
}

DEFAULT_TARGET = NIFTY_CONFIG["target"]
DEFAULT_SL = NIFTY_CONFIG["sl"]
DEFAULT_TSL_STEP = NIFTY_CONFIG["tsl"]
DEFAULT_TP_TSL = NIFTY_CONFIG["pt"]

# RSIM Strategy Settings
RSIM_RSI_UP = 70
RSIM_RSI_DOWN = 30
RSIM_MOMENTUM = 5
RSIM_SUSTAIN_TICKS = 3

# LTP Logger feature
ENABLE_LTP_LOGGER = False

# Capital Tracking
INITIAL_CAPITAL_DEFAULT = 8000.0  # Initial capital if no history exists
CAPITAL_HISTORY_FILE = "capital_history.csv"
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", INITIAL_CAPITAL_DEFAULT))
CAPITAL_TOPUP = float(os.getenv("CAPITAL_TOPUP", "0"))  # One-time adjustment (Add money: +5000, Withdraw: -5000)
PNL_RESET_DATE = os.getenv("PNL_RESET_DATE", "")
MAX_DAILY_LOSS_COUNT = int(os.getenv("MAX_DAILY_LOSS_COUNT", "50"))

# UI & Strategy refresh interval in milliseconds
REFRESH_INTERVAL_MS = 1000
