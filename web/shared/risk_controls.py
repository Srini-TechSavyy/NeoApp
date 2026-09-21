import json
import os
import time
from datetime import datetime
from typing import Dict, List, Tuple

from common.config import (
    BUY_DISABLE_DURATION,
    BUY_DISABLE_LOSS_COUNT,
    BUY_DISABLE_MAX_PROFIT,
    MAX_DAILY_LOSS_COUNT,
    PROGRESSIVE_LOSS_CONFIG,
)

BUY_DISABLED_FILE = os.getenv("BUY_DISABLED_FILE", "buy_disabled.json")


def _load_state() -> Dict:
    if not os.path.exists(BUY_DISABLED_FILE):
        return {}
    try:
        with open(BUY_DISABLED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(data: Dict) -> None:
    with open(BUY_DISABLED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _week_bounds():
    today = datetime.now().date()
    monday = today.fromordinal(today.toordinal() - today.weekday())
    friday = today.fromordinal(monday.toordinal() + 4)
    return monday, friday


def _week_trades(completed_trades: List[Dict]) -> List[Dict]:
    monday, friday = _week_bounds()
    out = []
    for t in completed_trades:
        st = t.get("sell_time") or t.get("buy_time")
        if not st or not hasattr(st, "date"):
            continue
        d = st.date()
        if monday <= d <= friday:
            out.append(t)
    return out


def evaluate_risk_state(completed_trades: List[Dict], last_exit_epoch: float = 0.0, cool_off_seconds: int = 0) -> Dict:
    now = time.time()
    today_str = datetime.now().strftime("%Y-%m-%d")
    weekly = _week_trades(completed_trades)

    net_pnl = sum(t.get("net_pnl", 0) for t in weekly)
    loss_amount = -net_pnl

    state = _load_state()
    if state.get("date") != today_str:
        state = {}

    current_label = state.get("last_trade_id")
    highest_threshold = int(state.get("highest_threshold", 0) or 0)
    disabled_until = float(state.get("disabled_until", 0) or 0)

    # Highest progressive threshold
    sorted_cfg = sorted(PROGRESSIVE_LOSS_CONFIG, key=lambda x: x[0], reverse=True)
    app_t, app_d = 0, 0
    for threshold, duration_mins in sorted_cfg:
        if loss_amount >= threshold:
            app_t, app_d = int(threshold), int(duration_mins)
            break

    reason = ""

    if app_t > 0 and app_t > highest_threshold:
        highest_threshold = app_t
        disabled_until = now + (app_d * 60)
        current_label = f"max_loss_{app_t}"
        reason = current_label
        _save_state({
            "disabled_until": disabled_until,
            "last_trade_id": current_label,
            "date": today_str,
            "highest_threshold": highest_threshold,
        })

    if net_pnl >= BUY_DISABLE_MAX_PROFIT:
        if current_label != "max_profit":
            disabled_until = now + 86400
            current_label = "max_profit"
            reason = current_label
            _save_state({
                "disabled_until": disabled_until,
                "last_trade_id": current_label,
                "date": today_str,
                "highest_threshold": highest_threshold,
            })

    daily_losses = [t for t in weekly if t.get("net_pnl", 0) < 0]
    if len(daily_losses) >= MAX_DAILY_LOSS_COUNT:
        if current_label != "max_loss_count":
            disabled_until = now + 86400
            current_label = "max_loss_count"
            reason = current_label
            _save_state({
                "disabled_until": disabled_until,
                "last_trade_id": current_label,
                "date": today_str,
                "highest_threshold": highest_threshold,
            })

    if len(weekly) >= BUY_DISABLE_LOSS_COUNT:
        last_n = weekly[-BUY_DISABLE_LOSS_COUNT:]
        last_trade_time = last_n[-1].get("sell_time") or last_n[-1].get("buy_time")
        last_trade_ts = str(last_trade_time.timestamp()) if hasattr(last_trade_time, "timestamp") else str(last_trade_time)
        if all(t.get("net_pnl", 0) < 0 for t in last_n) and last_trade_ts != current_label:
            disabled_until = now + BUY_DISABLE_DURATION
            current_label = last_trade_ts
            reason = "consecutive_losses"
            _save_state({
                "disabled_until": disabled_until,
                "last_trade_id": current_label,
                "date": today_str,
                "highest_threshold": highest_threshold,
            })

    # Cool-off (between manual trades; should never exceed cool_off_seconds)
    cooloff_remaining = 0
    if cool_off_seconds > 0 and last_exit_epoch > 0:
        elapsed = now - last_exit_epoch
        if elapsed >= 0 and elapsed < cool_off_seconds:
            cooloff_remaining = int(cool_off_seconds - elapsed)

    # Expire lock if elapsed and no active threshold trigger
    if disabled_until > 0 and now >= disabled_until and app_t == 0 and net_pnl < BUY_DISABLE_MAX_PROFIT:
        try:
            d = _load_state()
            d["disabled_until"] = 0
            _save_state(d)
            disabled_until = 0
        except Exception:
            pass

    locked = disabled_until > now
    if locked and not reason:
        reason = str(current_label or "locked")

    return {
        "buy_allowed": (not locked) and cooloff_remaining == 0,
        "locked": locked,
        "reason": reason,
        "disabled_until": disabled_until,
        "cooloff_remaining": cooloff_remaining,
        "weekly_net_pnl": round(net_pnl, 2),
        "highest_threshold": highest_threshold,
        "weekly_trades": len(weekly),
    }
