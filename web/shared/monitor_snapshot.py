from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple


def _current_week_window(today: Optional[date] = None) -> Tuple[date, date]:
    if today is None:
        today = datetime.now().date()
    monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)
    return monday, friday


def _trade_time(trade: Dict):
    return trade.get("sell_time") or trade.get("buy_time")


def _trade_date(trade: Dict) -> Optional[date]:
    td = trade.get("trade_date")
    if isinstance(td, datetime):
        return td.date()
    if isinstance(td, date):
        return td
    if isinstance(td, str):
        try:
            return datetime.fromisoformat(td).date()
        except ValueError:
            pass

    t = _trade_time(trade)
    if not t:
        return None
    if isinstance(t, datetime):
        return t.date()
    if isinstance(t, date):
        return t
    if isinstance(t, str):
        try:
            return datetime.fromisoformat(t).date()
        except ValueError:
            pass
    if hasattr(t, "date"):
        return t.date()
    return None


def _filter_weekday_trades(
    completed_trades: List[Dict], today: Optional[date] = None
) -> Tuple[List[Dict], date, date]:
    if today is None:
        today = datetime.now().date()
    monday, friday = _current_week_window(today)
    weekly = []

    for trade in completed_trades:
        d = _trade_date(trade)
        if not d:
            continue
        if monday <= d <= friday:
            weekly.append(trade)

    return weekly, monday, friday


def build_monitor_snapshot(
    completed_trades: List[Dict],
    open_position: Optional[Dict] = None,
    for_date: Optional[date] = None,
) -> Dict:
    today = for_date if for_date is not None else datetime.now().date()
    weekly_trades, monday, friday = _filter_weekday_trades(completed_trades, today)

    pnls = [round(t.get("net_pnl", 0)) for t in weekly_trades]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    recent_pnls = pnls[-5:]
    streak = ["P" if p > 0 else ("L" if p < 0 else "BE") for p in recent_pnls]

    # Daily trading performance (counts strictly completed trades on the current exchange trading date)
    today_trades = [t for t in completed_trades if _trade_date(t) == today]
    today_wins = 0
    today_losses = 0
    today_break_even = 0
    for t in today_trades:
        net = round(float(t.get("net_pnl", 0) or 0), 2)
        if net > 0:
            today_wins += 1
        elif net < 0:
            today_losses += 1
        else:
            today_break_even += 1
    today_total = today_wins + today_losses + today_break_even

    gross_pnl = round(sum(t.get("gross_pnl", 0) for t in weekly_trades), 2)
    net_pnl = round(sum(t.get("net_pnl", 0) for t in weekly_trades), 2)
    charges = round(sum(t.get("charges", 0) for t in weekly_trades), 2)
    day_pnl = round(
        sum(
            t.get("net_pnl", 0)
            for t in weekly_trades
            if _trade_date(t) == today
        ),
        2,
    )

    unrealized_pnl = 0.0
    if open_position and isinstance(open_position, dict):
        unrealized_pnl = round(float(open_position.get("unrealized_pnl", 0) or 0), 2)

    total_gross = round(gross_pnl + unrealized_pnl, 2)
    total_net = round(net_pnl + unrealized_pnl, 2)
    day_total = round(day_pnl + unrealized_pnl, 2)

    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "period": {
            "type": "weekly",
            "week_start": str(monday),
            "week_end": str(friday),
        },
        "trade_stats": {
            "no_of_trades": len(weekly_trades),
            "wins": wins,
            "losses": losses,
            "win_rate": round((wins / len(pnls)) * 100, 2) if pnls else 0.0,
            "recent": recent_pnls,
            "streak": streak,
            "today_wins": today_wins,
            "today_losses": today_losses,
            "today_break_even": today_break_even,
            "today_total": today_total,
        },
        "today_trade_stats": {
            "wins": today_wins,
            "losses": today_losses,
            "break_even": today_break_even,
            "total_trades": today_total,
        },
        "pnl": {
            "gross": gross_pnl,
            "net": net_pnl,
            "day": day_pnl,
            "charges": charges,
            "unrealized": unrealized_pnl,
            "total_gross": total_gross,
            "total_net": total_net,
            "day_total": day_total,
        },
    }
