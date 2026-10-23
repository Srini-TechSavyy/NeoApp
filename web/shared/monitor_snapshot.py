from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple


def _current_week_window() -> Tuple[datetime.date, datetime.date]:
    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)
    return monday, friday


def _trade_time(trade: Dict):
    return trade.get("sell_time") or trade.get("buy_time")


def _trade_date(trade: Dict):
    t = _trade_time(trade)
    if not t or not hasattr(t, "date"):
        return None
    return t.date()


def _filter_weekday_trades(completed_trades: List[Dict]) -> Tuple[List[Dict], datetime.date, datetime.date]:
    monday, friday = _current_week_window()
    weekly = []

    for trade in completed_trades:
        d = _trade_date(trade)
        if not d:
            continue
        if monday <= d <= friday:
            weekly.append(trade)

    return weekly, monday, friday


def build_monitor_snapshot(completed_trades: List[Dict], open_position: Optional[Dict] = None) -> Dict:
    weekly_trades, monday, friday = _filter_weekday_trades(completed_trades)
    today = datetime.now().date()

    pnls = [round(t.get("net_pnl", 0)) for t in weekly_trades]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)

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
            "recent": pnls[-5:],
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
