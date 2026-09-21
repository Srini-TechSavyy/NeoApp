import os
import sys
import time
import logging
import csv
from datetime import datetime

# Ensure project root import path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from common.orders import ensure_login
import common.orders as orders_module
from common.orders import detect_exchange_segment
from common.scrip_master import find_token_for_trading_symbol, get_lot_size_from_scrip_master, load_scrip_master_csv
from common.config import COOL_OFF_PERIOD
from common.time_utils import market_time_to_epoch
from indicator.scalping_indicator import LiveScalpingManager, RSIMomentumStrategy
from monitor.pnl_engine import PositionPnLEngine, parse_api_orders
from web.shared.monitor_snapshot import build_monitor_snapshot
from web.shared.risk_controls import evaluate_risk_state
from web.shared.symbol_helpers import get_underlying_index, suggest_option_symbol
from web.shared.state_store import write_snapshot
from web.shared.trade_orders import refresh_pending_orders
from web.shared.trading_actions import execute_market_action

POLL_SECONDS = int(os.getenv("WEB_POLL_SECONDS", "5"))
MAX_BACKOFF_SECONDS = int(os.getenv("WEB_MAX_BACKOFF_SECONDS", "60"))
logger = logging.getLogger("neoapp.web.worker")
logging.basicConfig(level=os.getenv("WEB_LOG_LEVEL", "INFO"))

AUTO_BUY_ENABLED = os.getenv("AUTO_BUY_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
AUTO_SELL_ENABLED = os.getenv("AUTO_SELL_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
TRADING_ENABLED = os.getenv("TRADING_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
AUTO_LOTS = int(os.getenv("AUTO_LOTS", "1"))

TARGET_POINTS = float(os.getenv("AUTO_TARGET", "2"))
SL_POINTS = float(os.getenv("AUTO_SL", "1.5"))
TRAIL_STEP = float(os.getenv("AUTO_TSL_STEP", "1"))
PROFIT_TRAIL_STEP = float(os.getenv("AUTO_PT_STEP", "1"))

AUTO_BASE_INDEX = os.getenv("AUTO_BASE_INDEX", "NIFTY")
AUTO_STRIKE_OFFSET = int(os.getenv("AUTO_STRIKE_OFFSET", "0"))


def _now() -> str:
    return datetime.now().isoformat()


def _reset_login_session():
    try:
        orders_module._client = None
    except Exception:
        pass


def _is_session_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    markers = [
        "401",
        "403",
        "token",
        "session",
        "unauthor",
        "forbidden",
        "access denied",
    ]
    return any(m in msg for m in markers)


def _normalize_list_payload(payload):
    if isinstance(payload, dict):
        data = payload.get("data", [])
        return data if isinstance(data, list) else []
    if isinstance(payload, list):
        return payload
    return []


def _extract_open_position(client):
    data = _normalize_list_payload(client.positions())
    for p in data:
        sym = str(p.get("trdSym", "")).upper()
        exch = str(p.get("exch", "")).lower()
        fl_buy = float(p.get("flBuyQty", 0) or 0)
        fl_sell = float(p.get("flSellQty", 0) or 0)
        qty = int(float(p.get("netQty", fl_buy - fl_sell) or 0))
        if qty == 0:
            continue
        if "-EQ" in sym:
            continue
        if "fo" in exch or any(x in sym for x in ["NIFTY", "SENSEX", "BANKNIFTY", "FINNIFTY", "BANKEX"]):
            avg_p = p.get("avgPrc") or p.get("buyAvgPrc") or p.get("buyAvg")
            if (not avg_p or float(avg_p) == 0) and fl_buy > 0:
                buy_amt = float(p.get("buyAmt", 0) or 0)
                avg_p = buy_amt / fl_buy if fl_buy > 0 else 0
            return {
                "symbol": sym,
                "qty": qty,
                "entry_price": float(avg_p or 0),
                "exchange": exch,
            }
    return None


def _fetch_index_ltp(client, symbol_hint: str):
    idx_token, idx_exch, idx_name = get_underlying_index(symbol_hint)
    if not idx_token:
        idx_token, idx_exch, idx_name = get_underlying_index(AUTO_BASE_INDEX)
    if not idx_token:
        return 0.0, None, None, None

    quote_resp = client.quotes(instrument_tokens=[{"instrument_token": idx_token, "exchange_segment": idx_exch}], quote_type="ltp")
    ltp = 0.0
    if isinstance(quote_resp, list) and quote_resp:
        ltp = float(quote_resp[0].get("ltp", 0) or 0)
    elif isinstance(quote_resp, dict):
        data = quote_resp.get("data", [])
        if isinstance(data, list) and data:
            ltp = float(data[0].get("ltp", 0) or 0)
        elif "ltp" in quote_resp:
            ltp = float(quote_resp.get("ltp", 0) or 0)

    return ltp, idx_token, idx_exch, idx_name


def _fetch_option_ltp(client, trading_symbol: str) -> float:
    token = find_token_for_trading_symbol(trading_symbol)
    if not token:
        return 0.0
    exch = detect_exchange_segment(trading_symbol)
    q = client.quotes(instrument_tokens=[{"instrument_token": token, "exchange_segment": exch}], quote_type="ltp")
    if isinstance(q, list) and q:
        return float(q[0].get("ltp", 0) or 0)
    if isinstance(q, dict):
        data = q.get("data", [])
        if isinstance(data, list) and data:
            return float(data[0].get("ltp", 0) or 0)
    return 0.0


def _log_new_trades_to_csv(completed_trades, seen_buy_ids):
    if not completed_trades:
        return 0
    os.makedirs("logs", exist_ok=True)
    filename = f"logs/trades_web_{datetime.now().strftime('%Y-%m-%d')}.csv"
    file_exists = os.path.exists(filename)
    headers = [
        "Symbol", "Date", "Buy Time", "Sell Time", "Qty", "Buy Price", "Sell Price",
        "Gross PnL", "Charges", "Net PnL", "Buy ID", "Sell ID", "Order Source",
    ]
    count = 0
    with open(filename, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        if not file_exists:
            writer.writeheader()
        for t in completed_trades:
            bid = str(t.get("buy_order_id", ""))
            if not bid or bid in seen_buy_ids:
                continue
            writer.writerow(
                {
                    "Symbol": t.get("symbol"),
                    "Date": t.get("trade_date"),
                    "Buy Time": t.get("buy_time"),
                    "Sell Time": t.get("sell_time"),
                    "Qty": t.get("buy_qty"),
                    "Buy Price": t.get("buy_price"),
                    "Sell Price": t.get("sell_price"),
                    "Gross PnL": t.get("gross_pnl"),
                    "Charges": t.get("charges"),
                    "Net PnL": t.get("net_pnl"),
                    "Buy ID": bid,
                    "Sell ID": t.get("sell_order_id"),
                    "Order Source": t.get("order_source", "NA"),
                }
            )
            seen_buy_ids.add(bid)
            count += 1
    return count


def _suggest_symbol_for_signal(index_name: str, idx_ltp: float, signal: str):
    option_type = "CE"
    if "BEARISH" in signal:
        option_type = "PE"

    base = AUTO_BASE_INDEX
    if index_name and "BANK" in str(index_name).upper() and "NIFTY" in str(index_name).upper():
        base = "BANKNIFTY"
    if index_name and "SENSEX" in str(index_name).upper():
        base = "SENSEX"

    return suggest_option_symbol(base, idx_ltp, option_type, AUTO_STRIKE_OFFSET)


def _run_once(client, manager: LiveScalpingManager, runtime_state: dict):
    pending_meta = {}
    pending_orders = refresh_pending_orders(client, latency_meta=pending_meta)
    if pending_meta:
        logger.info(
            "Worker pending order refresh: count=%s updates=%s ms=%s",
            pending_meta.get("pending_order_count"),
            pending_meta.get("pending_order_updates"),
            pending_meta.get("pending_order_refresh_ms"),
        )

    report = client.order_report()
    report_data = report.get("data", []) if isinstance(report, dict) else (report or [])

    trades = parse_api_orders(report_data if isinstance(report_data, list) else [])
    trades.sort(key=lambda x: x.time)

    engine = PositionPnLEngine()
    for trade in trades:
        engine.add_trade(trade)

    last_exit_time = 0.0
    if engine.completed_trades:
        st = engine.completed_trades[-1].get("sell_time") or engine.completed_trades[-1].get("buy_time")
        last_exit_time = market_time_to_epoch(st) if st is not None else 0.0

    open_pos = _extract_open_position(client)

    symbol_for_index = open_pos["symbol"] if open_pos else AUTO_BASE_INDEX
    idx_ltp, _, _, idx_name = _fetch_index_ltp(client, symbol_for_index)
    if idx_ltp > 0:
        manager.add_ltp(idx_ltp, idx_name or AUTO_BASE_INDEX)
    indicator_data = manager.get_signal()

    risk = evaluate_risk_state(engine.completed_trades, last_exit_time, COOL_OFF_PERIOD)

    auto_info = {
        "auto_buy_enabled": AUTO_BUY_ENABLED,
        "auto_sell_enabled": AUTO_SELL_ENABLED,
        "trading_enabled": TRADING_ENABLED,
        "base_index": AUTO_BASE_INDEX,
        "strike_offset": AUTO_STRIKE_OFFSET,
        "target": TARGET_POINTS,
        "sl": SL_POINTS,
        "trail_step": TRAIL_STEP,
        "profit_trail_step": PROFIT_TRAIL_STEP,
    }

    action_msg = ""
    # AUTO BUY
    if TRADING_ENABLED and AUTO_BUY_ENABLED and not open_pos and risk.get("buy_allowed"):
        sig = str(indicator_data.get("signal", ""))
        if sig in ("BULLISH", "BEARISH") and idx_ltp > 0:
            try:
                symbol = _suggest_symbol_for_signal(idx_name or AUTO_BASE_INDEX, idx_ltp, sig)
                resp = execute_market_action(symbol, AUTO_LOTS, "BUY", enforce_single_position=True)
                action_msg = f"AUTO BUY executed: {symbol} ({resp.get('order_id')})"
                runtime_state["max_price_seen"] = resp.get("execution_price") or 0
            except Exception as e:
                action_msg = f"AUTO BUY failed: {e}"

    # AUTO SELL
    if TRADING_ENABLED and AUTO_SELL_ENABLED and open_pos:
        symbol = open_pos["symbol"]
        entry = float(open_pos.get("entry_price") or 0)
        cur_ltp = _fetch_option_ltp(client, symbol)
        if entry > 0 and cur_ltp > 0:
            profit = cur_ltp - entry
            max_seen = float(runtime_state.get("max_price_seen", entry))
            if cur_ltp > max_seen:
                max_seen = cur_ltp
            runtime_state["max_price_seen"] = max_seen

            trail_gain = max(0.0, max_seen - entry) if TRAIL_STEP > 0 else 0.0
            effective_sl = SL_POINTS - trail_gain

            should_exit = False
            reason = ""
            if profit >= TARGET_POINTS:
                if PROFIT_TRAIL_STEP > 0:
                    max_profit = max_seen - entry
                    if profit <= (max_profit - PROFIT_TRAIL_STEP):
                        should_exit = True
                        reason = "Trail-Profit"
                else:
                    should_exit = True
                    reason = "Target"
            elif profit <= -effective_sl:
                should_exit = True
                reason = "SL"

            if should_exit:
                try:
                    lot_size = max(int(get_lot_size_from_scrip_master(symbol, default=1)), 1)
                    lots = max(1, round(abs(int(open_pos.get("qty", 0))) / lot_size))
                    resp = execute_market_action(symbol, int(lots), "SELL", enforce_single_position=False)
                    action_msg = f"AUTO EXIT ({reason}) executed: {symbol} ({resp.get('order_id')})"
                    runtime_state["max_price_seen"] = 0
                except Exception as e:
                    action_msg = f"AUTO EXIT failed: {e}"

    snapshot = build_monitor_snapshot(engine.completed_trades)
    snapshot["healthy"] = True
    snapshot["message"] = "Worker cycle completed"
    snapshot["indicator"] = {
        "signal": indicator_data.get("signal"),
        "rsi": indicator_data.get("rsi"),
        "momentum": indicator_data.get("momentum"),
        "ema": indicator_data.get("ema"),
        "roc": indicator_data.get("roc"),
        "bb_width": indicator_data.get("bb_width"),
        "sr": indicator_data.get("sr", {}),
        "sideways": indicator_data.get("sideways"),
        "exhausted": indicator_data.get("exhausted"),
        "index_name": idx_name,
        "index_ltp": idx_ltp,
    }
    snapshot["position"] = open_pos or {"symbol": None, "qty": 0, "entry_price": 0}
    snapshot["risk"] = risk
    snapshot["automation"] = auto_info
    snapshot["last_action"] = action_msg
    snapshot["pending_orders"] = pending_orders
    snapshot["source"] = "worker"
    snapshot["poll_seconds"] = POLL_SECONDS

    logged = _log_new_trades_to_csv(engine.completed_trades, runtime_state.setdefault("seen_buy_ids", set()))
    snapshot["journal"] = {"new_rows": logged}

    write_snapshot(snapshot)
    return snapshot


def _bootstrap_client():
    while True:
        try:
            load_scrip_master_csv()
            return ensure_login()
        except Exception as exc:
            logger.exception("Worker bootstrap failed, retrying")
            write_snapshot(
                {
                    "status": "error",
                    "healthy": False,
                    "timestamp": _now(),
                    "source": "worker",
                    "message": "Worker bootstrap failed",
                    "error": str(exc),
                }
            )
            time.sleep(5)


def main():
    logger.info("Starting web worker (poll=%ss)", POLL_SECONDS)
    client = _bootstrap_client()
    manager = LiveScalpingManager(strategy=RSIMomentumStrategy())
    runtime_state = {"max_price_seen": 0.0, "seen_buy_ids": set()}
    consecutive_failures = 0
    last_success_at = None

    while True:
        cycle_started = time.time()
        try:
            _run_once(client, manager, runtime_state)
            consecutive_failures = 0
            last_success_at = _now()
            logger.info("Worker cycle success")
        except Exception as exc:
            consecutive_failures += 1
            logger.exception("Worker cycle failed (count=%s)", consecutive_failures)

            if _is_session_error(exc):
                _reset_login_session()
                try:
                    client = ensure_login()
                except Exception as relogin_exc:
                    logger.exception("Worker relogin failed: %s", relogin_exc)

            error_snapshot = {
                "status": "error",
                "healthy": False,
                "timestamp": datetime.now().isoformat(),
                "message": "Worker cycle failed",
                "error": str(exc),
                "source": "worker",
                "consecutive_failures": consecutive_failures,
                "last_success_at": last_success_at,
                "poll_seconds": POLL_SECONDS,
            }
            write_snapshot(error_snapshot)
            backoff = min(POLL_SECONDS * (2 ** min(consecutive_failures, 4)), MAX_BACKOFF_SECONDS)
            time.sleep(backoff)
            continue

        elapsed = time.time() - cycle_started
        sleep_for = max(POLL_SECONDS - elapsed, 0)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
