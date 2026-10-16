"""NSE equity segment trading calendar (weekends + exchange holidays)."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import FrozenSet, Optional

from common.time_utils import MARKET_TZ, _as_ist
from common.utils import get_resource_path

logger = logging.getLogger(__name__)

_HOLIDAYS_PATH = "data/nse_trading_holidays.json"


@lru_cache(maxsize=1)
def _load_holiday_dates() -> FrozenSet[date]:
    path = get_resource_path(_HOLIDAYS_PATH)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.warning(
            "NSE holiday calendar missing at %s; treating exchange holidays as empty (weekends only)",
            path,
        )
        return frozenset()
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning(
            "NSE holiday calendar unreadable at %s (%s); treating exchange holidays as empty",
            path,
            exc,
        )
        return frozenset()

    raw = data.get("holidays") or []
    out = set()
    for item in raw:
        if not item or str(item).startswith("_"):
            continue
        try:
            out.add(date.fromisoformat(str(item).strip()))
        except ValueError:
            logger.warning("Skipping invalid NSE holiday date entry: %s", item)
    return frozenset(out)


def reload_holiday_calendar() -> None:
    """Clear cached holidays (tests)."""
    _load_holiday_dates.cache_clear()


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def is_exchange_holiday(d: date) -> bool:
    return d in _load_holiday_dates()


def is_trading_day(d: date) -> bool:
    return not is_weekend(d) and not is_exchange_holiday(d)


def previous_trading_day(d: date) -> date:
    cur = d
    while True:
        cur = cur - timedelta(days=1)
        if is_trading_day(cur):
            return cur


def next_trading_day(d: date) -> date:
    cur = d
    while True:
        cur = cur + timedelta(days=1)
        if is_trading_day(cur):
            return cur


def ist_today(now: Optional[datetime] = None) -> date:
    return _as_ist(now).date()


def market_close_on_date(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 15, 30, 0, tzinfo=MARKET_TZ)
