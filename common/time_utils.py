from datetime import datetime
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
