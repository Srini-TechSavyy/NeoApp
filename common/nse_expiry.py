"""NIFTY index options expiry schedule (NSE rules + holiday adjustment)."""
from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import List, Optional

from common.config import _format_neo_expiry
from common.nse_calendar import (
    is_trading_day,
    ist_today,
    market_close_on_date,
    previous_trading_day,
)
from common.time_utils import _as_ist

NIFTY_EXPIRY_WEEKDAY = 1  # Tuesday


def nominal_weekly_tuesday(on_or_after: date) -> date:
    """First Tuesday on or after the given calendar date."""
    cur = on_or_after
    while cur.weekday() != NIFTY_EXPIRY_WEEKDAY:
        cur += timedelta(days=1)
    return cur


def nominal_monthly_expiry(year: int, month: int) -> date:
    """Last Tuesday of the calendar month."""
    last_day = monthrange(year, month)[1]
    cur = date(year, month, last_day)
    while cur.weekday() != NIFTY_EXPIRY_WEEKDAY:
        cur -= timedelta(days=1)
    return cur


def effective_expiry_date(nominal: date) -> date:
    """If nominal expiry is not a trading day, prepone to previous trading day."""
    if is_trading_day(nominal):
        return nominal
    return previous_trading_day(nominal)


def expiry_date_to_token(expiry_date: date) -> str:
    """Kotak/NSE F&O trading symbol expiry segment for the effective expiry date."""
    dt = datetime(expiry_date.year, expiry_date.month, expiry_date.day)
    return _format_neo_expiry(dt)


def _collect_nominal_expiries(start: date, months_ahead: int) -> List[date]:
    nominals: set[date] = set()
    cur = start
    end_month = (start.year * 12 + start.month - 1) + months_ahead
    end_year = end_month // 12
    end_mon = end_month % 12 + 1

    y, m = start.year, start.month
    while (y, m) <= (end_year, end_mon):
        nominals.add(nominal_monthly_expiry(y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1

    tuesday = nominal_weekly_tuesday(start)
    limit = start + timedelta(days=7 * 20)
    while tuesday <= limit:
        nominals.add(tuesday)
        tuesday += timedelta(days=7)

    return sorted(nominals)


def iter_upcoming_nifty_expiry_dates(
    from_date: Optional[date] = None,
    limit: int = 16,
) -> List[date]:
    """Holiday-adjusted upcoming NIFTY expiry dates, sorted ascending."""
    start = from_date or ist_today()
    effective: dict[date, date] = {}
    for nominal in _collect_nominal_expiries(start, months_ahead=4):
        eff = effective_expiry_date(nominal)
        if eff >= start:
            effective[eff] = nominal
    ordered = sorted(effective.keys())
    return ordered[:limit]


def next_nifty_expiry_date(now: Optional[datetime] = None) -> date:
    """
    Nearest tradeable NIFTY expiry date (IST), respecting 3:30 PM on expiry day.
    """
    ist = _as_ist(now)
    today = ist.date()
    candidates = iter_upcoming_nifty_expiry_dates(today, limit=24)
    if not candidates:
        return nominal_weekly_tuesday(today)

    first = candidates[0]
    if first == today:
        if ist < market_close_on_date(today):
            return first
        rest = [d for d in candidates if d > today]
        return rest[0] if rest else nominal_weekly_tuesday(today + timedelta(days=7))

    return first


def get_next_nifty_expiry_token(now: Optional[datetime] = None) -> str:
    expiry_date = next_nifty_expiry_date(now)
    return expiry_date_to_token(expiry_date)


def theoretical_expiry_tokens(from_date: Optional[date] = None, limit: int = 24) -> List[str]:
    tokens = []
    seen = set()
    for d in iter_upcoming_nifty_expiry_dates(from_date, limit=limit):
        tok = expiry_date_to_token(d)
        if tok not in seen:
            seen.add(tok)
            tokens.append(tok)
    return tokens
