"""1-minute OHLC candle aggregation (minute-boundary semantics aligned with bot/neobot.py)."""

from datetime import datetime
from typing import List, Optional


def minute_bucket(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def update_minute_candle(
    candles: List[dict],
    ltp: float,
    now: Optional[datetime] = None,
) -> bool:
    """
    Append or update the forming 1-minute candle for `ltp`.

    Returns True when a prior minute bucket just closed (minute rolled over).
    The newly closed candle is candles[-2] after this call; candles[-1] is forming.
    """
    now = now or datetime.now()
    bucket = minute_bucket(now)
    closed = False

    if not candles or candles[-1]["time"] != bucket:
        closed = bool(candles)
        candles.append(
            {
                "time": bucket,
                "open": ltp,
                "high": ltp,
                "low": ltp,
                "close": ltp,
            }
        )
    else:
        c = candles[-1]
        c["high"] = max(c["high"], ltp)
        c["low"] = min(c["low"], ltp)
        c["close"] = ltp

    return closed


def completed_candles(candles: List[dict]) -> List[dict]:
    """All fully closed buckets; excludes the currently forming candle."""
    if len(candles) <= 1:
        return []
    return candles[:-1]
