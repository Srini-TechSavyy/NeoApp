from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("Asia/Kolkata")


def market_time_to_epoch(dt: Optional[datetime]) -> float:
    """Convert broker trade timestamps to UTC epoch (timestamps are IST)."""
    if dt is None:
        return 0.0
    if dt.tzinfo is not None:
        return float(dt.timestamp())
    return float(dt.replace(tzinfo=MARKET_TZ).timestamp())


def _as_ist(dt: Optional[datetime] = None) -> datetime:
    if dt is None:
        return datetime.now(MARKET_TZ)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=MARKET_TZ)
    return dt.astimezone(MARKET_TZ)


def next_midnight_ist_epoch(now: Optional[datetime] = None) -> float:
    """Epoch seconds when the next calendar day begins in Asia/Kolkata."""
    ist = _as_ist(now)
    next_day = ist.date() + timedelta(days=1)
    midnight = datetime(next_day.year, next_day.month, next_day.day, 0, 0, 0, tzinfo=MARKET_TZ)
    return float(midnight.timestamp())


def format_ist_datetime(epoch_seconds: float) -> str:
    """Human-readable IST timestamp for UI and API messages."""
    dt = datetime.fromtimestamp(epoch_seconds, tz=MARKET_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S IST")
