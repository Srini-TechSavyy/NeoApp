import tkinter as tk
from tkinter import ttk, messagebox
import sys, os, re, difflib
import pandas as pd
from datetime import datetime, timedelta
import csv

import sys
import os

# Allow importing from parent directory
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.config import DEFAULT_TRADING_SYMBOL, BUY_DISABLE_DURATION, BUY_DISABLE_LOSS_COUNT, BUY_DISABLE_MAX_LOSS, BUY_DISABLE_MAX_PROFIT, PROGRESSIVE_LOSS_CONFIG, PROGRESSIVE_LOSS_CONFIG_DEFAULT, REFRESH_INTERVAL_MS, DEFAULT_TARGET, DEFAULT_SL, DEFAULT_TSL_STEP, DEFAULT_TP_TSL, COOL_OFF_PERIOD, TELEGRAM_COOL_OFF, REMOTE_CONFIG_URL, PROGRESSIVE_LOSS_URL, NIFTY_CONFIG, SENSEX_CONFIG, INITIAL_CAPITAL, CAPITAL_HISTORY_FILE, CAPITAL_TOPUP, PNL_RESET_DATE, MAX_DAILY_LOSS_COUNT, RSIM_RSI_UP, RSIM_RSI_DOWN, RSIM_SUSTAIN_TICKS, get_next_expiry
from common.utils import log_with_callback, run_bg, fetch_remote_config, fetch_remote_json, get_resource_path, send_telegram_msg
from common.scrip_master import load_scrip_master_csv, find_token_for_trading_symbol
from common.orders import ensure_login as do_login, place_market_order, get_client, detect_exchange_segment, detect_strike_step
from indicator.scalping_indicator import LiveScalpingManager, RSIMomentumStrategy
from monitor.pnl_engine import PositionPnLEngine, parse_api_orders
import json
import time

BUY_DISABLED_FILE = "buy_disabled.json"

HISTORY_FILE = "symbol_history.json"

# Local imports for modules still in the scalper package
# LTP helper functions removed/unused; no import required


# ---------------------------------------------------------
# STATE
# ---------------------------------------------------------
buy_active = False  # ✅ only one BUY at a time
buy_pending = False # ⏳ Track if a BUY order is currently being placed
exit_pending = False # ⌛ Track if an EXIT order is currently being placed
active_symbol = ""  # 🏷️ The exact symbol of the current open position
buy_disabled = False  # Track if buy is disabled due to losses
last_trade_count = 0 # Track saved trades
last_display_content = "" # Cache last displayed content to prevent flickering
scalp_manager = LiveScalpingManager(strategy=RSIMomentumStrategy())
auto_buy = False    # Automated entry status
auto_sell = False   # Automated exit status
last_buy_price = 0.0 # Entry price of current active position
max_price_seen = 0.0 # Peak price for trailing SL tracking
active_trade_metadata = {} # Snapshot of indicators at entry/exit
last_exit_reason = "" # Track last exit reason for logging
last_exit_time = 0   # ⏱️ Track when the last trade ended for cool-off period
last_mom_alert_time = 0 # ⏱️ Cooldown for Ultra Momentum Telegram alerts
mom_threshold_start = 0 # ⏱️ Track when momentum threshold was first crossed
# Capital & Rollover Initialization
def get_latest_capital():
    """Fetches the latest balance from history. 
    If today's entry already exists, we use today's LAST Closing Capital as the starting point 
    for this session's PnL calculations.
    """
    base = INITIAL_CAPITAL
    if os.path.exists(CAPITAL_HISTORY_FILE):
        try:
            df = pd.read_csv(CAPITAL_HISTORY_FILE)
            if not df.empty:
                # Ensure Added column compatibility
                if "Added" not in df.columns:
                    df["Added"] = 0.0

                df['Date'] = pd.to_datetime(df['Date']).dt.date
                today = datetime.now().date()
                today_entry = df[df['Date'] == today]
                if not today_entry.empty:
                    # ⚠️ FIX: On restart within the SAME day, use today's ORIGINAL Initial Capital.
                    # This allows the PnL engine (which rebuilds from ALL daily orders) to 
                    # correctly calculate the Daily % and Current Capital relative to day-start.
                    base = float(today_entry.iloc[0]["Initial Capital"])
                else:
                    last_row = df.iloc[-1]
                    closing = float(last_row["Closing Capital"])
                    added = float(last_row.get("Added", 0))
                    base = closing + added
        except Exception as e:
            print(f"Error reading capital history: {e}")
    return base + CAPITAL_TOPUP

def get_overall_pnl_summary(current_net_pnl=0.0):
    """Calculates total net PnL percentage since PNL_RESET_DATE.
    """
    total_pnl = current_net_pnl
    base_cap = pnl_engine.initial_capital
    if os.path.exists(CAPITAL_HISTORY_FILE):
        try:
            df = pd.read_csv(CAPITAL_HISTORY_FILE)
            if not df.empty:
                df['Date'] = pd.to_datetime(df['Date'])
                now = datetime.now()
                
                # History sum (excluding today)
                mask = (df['Date'].dt.date < now.date())
                
                if PNL_RESET_DATE:
                    try:
                        reset_dt = pd.to_datetime(PNL_RESET_DATE).date()
                        mask = mask & (df['Date'].dt.date >= reset_dt)
                        
                        # Find original capital from reset date to use as base
                        reset_row = df[df['Date'].dt.date == reset_dt]
                        if not reset_row.empty:
                            base_cap = float(reset_row.iloc[0]["Initial Capital"])
                    except: pass

                # Take ONLY the last entry for each date to avoid double-counting
                daily_summary = df[mask].groupby(df['Date'].dt.date).last()
                total_pnl += daily_summary['Net PnL'].sum()
                
                # Update base_cap to include any Added capital since reset
                if PNL_RESET_DATE:
                    total_added = daily_summary['Added'].sum() if 'Added' in daily_summary else 0
                    base_cap += total_added
        except Exception as e:
            print(f"Error calculating overall PnL: {e}")
            
    if base_cap == 0: return 0.0
    return round((total_pnl / base_cap) * 100, 2)


def get_current_week_window():
    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)
    return monday, friday


def filter_current_week_trades(completed_trades):
    monday, friday = get_current_week_window()
    weekly_trades = []

    for t in completed_trades:
        trade_time = t.get("sell_time") or t.get("buy_time")
        if not trade_time or not hasattr(trade_time, "date"):
            continue
        if monday <= trade_time.date() <= friday:
            weekly_trades.append(t)

    return weekly_trades, monday, friday

pnl_engine = PositionPnLEngine(initial_capital=get_latest_capital()) 
eod_saved_today = False 
market_sideways = False # Track if market is currently sideways
market_exhausted = False # Track if market is currently exhausted

# Store last index data for immediate UI updates
last_idx_ltp = 0
last_idx_name = ""

# Track last logged values to prevent duplicate logs
last_logged_strike = None
last_logged_params_symbol = ""


# ---------------------------------------------------------
# ROOT
# ---------------------------------------------------------
root = tk.Tk()
root.title("SCALPER PRO")
root.geometry("850x340")
root.resizable(True, True)
root.configure(bg="#f5f5f5")
icon = tk.PhotoImage(file=get_resource_path("assets/scalper2.png"))
root.iconphoto(True, icon)

# ---------------------------------------------------------
# STYLE
# ---------------------------------------------------------
style = ttk.Style()
style.theme_use("clam")

style.configure(".", font=("Segoe UI", 11))
style.configure("TButton", padding=(6, 3))
style.configure("TEntry", padding=(6, 4))
style.configure("TFrame", background="#f5f5f5")

style.configure("Buy.TButton", background="#d1fae5", font=("Segoe UI", 11, "bold"))
style.map("Buy.TButton", background=[("active", "#a7f3d0")])

style.configure("Exit.TButton", background="#fee2e2", font=("Segoe UI", 11, "bold"))
style.map("Exit.TButton", background=[("active", "#fecaca")])

style.configure("CE.TButton", background="#e6f4ea", foreground="#166534")
style.configure("PE.TButton", background="#fdecea", foreground="#991b1b")

style.configure("Auto.TButton", background="#fef9c3")  # Default yellow-ish
style.configure("AutoOn.TButton", background="#4ade80", font=("Segoe UI", 11, "bold"))
style.configure("Sideways.TButton", background="#ffedd5", font=("Segoe UI", 11, "bold"))
style.map("Sideways.TButton", background=[("active", "#fed7aa")])

style.configure("Locked.TButton", background="#991b1b", foreground="white", font=("Segoe UI", 11, "bold"))
style.map("Locked.TButton", background=[("active", "#7f1d1d")])

def save_capital_to_csv(session_initial_cap, session_net_pnl, session_closing_cap, session_pct_change, session_trades):
    """Saves session results to capital_history.csv. 
    If a row for today already exists, it ACCUMULATES pnl and trades while preserving 
    today's original Initial Capital for overall % calculation.
    """
    global eod_saved_today
    try:
        now_str = datetime.now().strftime("%Y-%m-%d")
        
        # Read existing data
        rows = []
        file_exists = os.path.exists(CAPITAL_HISTORY_FILE)
        header = ["Date", "Initial Capital", "Added", "Net PnL", "Closing Capital", "% Change", "Trades"]
        
        updated = False
        if file_exists:
            with open(CAPITAL_HISTORY_FILE, "r", newline="") as f:
                reader = csv.reader(f)
                header = next(reader, header)
                for r in reader:
                    if r and r[0] == now_str:
                        # ⚠️ FIX: Upate (don't accumulate) because session_net_pnl is already the Daily Total
                        prev_initial = float(r[1])
                        prev_added = float(r[2])
                        
                        new_pnl = float(session_net_pnl)
                        new_trades = int(session_trades)
                        new_closing = round(prev_initial + prev_added + new_pnl, 2)
                        
                        base_for_pct = prev_initial + prev_added
                        new_pct = round((new_pnl / base_for_pct) * 100, 2) if base_for_pct != 0 else 0.0
                        
                        rows.append([now_str, prev_initial, prev_added, new_pnl, new_closing, new_pct, new_trades])
                        updated = True
                    else:
                        rows.append(r)
        
        if not updated:
            # New entry for today
            row_data = [now_str, session_initial_cap, 0, session_net_pnl, session_closing_cap, session_pct_change, session_trades]
            rows.append(row_data)
            
        with open(CAPITAL_HISTORY_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(rows)
            
        eod_saved_today = True
        log_with_callback(log_cb, f"🏁 Capital History Updated: Today's Net {new_pnl:+.2f} ({new_pct:+.2f}%)" if updated else f"🏁 Capital History Initialized: {session_closing_cap:.2f}")
    except Exception as e:
        log_with_callback(log_cb, f"⚠️ Save Error: {e}")

def on_closing():
    if messagebox.askokcancel("Quit", "Do you want to quit? (Capital will be saved)"):
        try:
            completed = pnl_engine.completed_trades
            net = round(sum(t["net_pnl"] for t in completed), 2)
            cur_cap = pnl_engine.get_current_capital()
            pct_pnl = pnl_engine.get_pnl_percentage()
            trades_count = len(completed)
            
            # Save if not already saved today
            if not eod_saved_today:
                save_capital_to_csv(pnl_engine.initial_capital, net, cur_cap, pct_pnl, trades_count)
            
            # Prepare Exit Summary Message
            overall_pct = get_overall_pnl_summary(net)
            summary_msg = (
                f"🏁 *NeoScalper Session Closed*\n\n"
                f"📅 Date: {datetime.now().strftime('%Y-%m-%d')}\n"
                f"🔢 Trades: {trades_count}\n"
                f"💰 Net PnL: {net:+.2f}\n"
                f"🏦 Capital: {cur_cap:.2f}\n"
                f"📊 Day %: {pct_pnl:+.2f}%\n"
                f"📈 Overall %: {overall_pct:+.2f}%"
            )
            # Send synchronously to ensure it goes out before app terminates
            send_telegram_msg(summary_msg)
        except Exception as e:
            print(f"Error during on_closing: {e}")
        root.destroy()

root.protocol("WM_DELETE_WINDOW", on_closing)

# ---------------------------------------------------------
# MAIN LAYOUT
# ---------------------------------------------------------
top_bar = ttk.Frame(root, padding=(8, 2))
top_bar.pack(fill="x", side="top")

date_time_label = ttk.Label(top_bar, text="", font=("Segoe UI", 10, "bold"))
date_time_label.pack(side="right")

main_container = ttk.Frame(root, padding=6)
main_container.pack(fill=tk.BOTH, expand=True)

# LEFT FRAME (Scalper)
frm = ttk.Frame(main_container, padding=6)
frm.pack(side="left", fill="both", expand=True)

# RIGHT FRAME (Monitor)
mon_frame = ttk.Frame(main_container, padding=6, relief="groove")
mon_frame.pack(side="right", fill="both", expand=True, padx=(10, 0))

ttk.Label(mon_frame, text="PnL Monitor", font=("Segoe UI", 12, "bold")).pack(anchor="n", pady=(0, 5))
mon_text = tk.Text(mon_frame, width=44, height=12, bg="#111827", fg="#e5e7eb", font=("Consolas", 10), state="disabled")
mon_text.pack(fill="both", expand=True)


# ---------------------------------------------------------
# SCALPER UI (Left)
# ---------------------------------------------------------
for i in range(4):
    frm.columnconfigure(i, pad=2)

# LOG BOX
log_box = tk.Text(
    frm, height=8, width=70,
    bg="#111827", fg="#e5e7eb",
    insertbackground="white",
    relief="flat",
    font=("Consolas", 10)
)
log_box.grid(row=6, column=0, columnspan=5, pady=(5, 0))

def log_cb(msg):
    def _update():
        log_box.insert(tk.END, msg + "\n")
        log_box.see(tk.END)
    root.after(0, _update)


# ---------------------------------------------------------
# SYMBOL HELPERS
# ---------------------------------------------------------

def get_underlying_index(symbol: str):
    """Maps trading symbol to its underlying index and exchange. Returns (token, exch, nickname)"""
    s = symbol.upper()
    if "NIFTY" in s:
        if "BANK" in s:
            return "Nifty Bank", "nse_cm", "BANKNIFTY"
        elif "FIN" in s:
            return "Nifty Fin Services", "nse_cm", "FINNIFTY"
        return "Nifty 50", "nse_cm", "NIFTY"
    elif "SENSEX" in s or "BSX" in s:
        return "SENSEX", "bse_cm", "SENSEX"
    elif "BANKEX" in s:
        return "BANKEX", "bse_cm", "BANKEX"
    return None, None, None


def parse_symbol_parts(symbol: str):
    """
    Robustly parses a symbol like NIFTY2621725600CE into parts.
    Handles variable length expiry (5 or 6 digits) correctly.
    Returns: (base, expiry, strike, opt_type) or None
    """
    s = symbol.strip().upper()
    
    # Try 6-digit match first (Greedy)
    m = re.search(r'^([A-Z]+?)([0-9]{6})(\d+)(CE|PE)$', s)
    if m:
        base, expiry, strike, opt_type = m.groups()
        # Validate Expiry: YYMMDD
        try:
            mm = int(expiry[2:4])
            dd = int(expiry[4:6])
            if 1 <= mm <= 12 and 1 <= dd <= 31:
                return base, expiry, int(strike), opt_type
        except:
            pass
            
    # Try 5-character match (2 digits + 3 letters for Monthly) 
    # Example: NIFTY26FEB... -> 26 (Year) + FEB (Month)
    m = re.search(r'^([A-Z]+?)([0-9]{2}[A-Z]{3})(\d+)(CE|PE)$', s)
    if m:
        base, expiry, strike, opt_type = m.groups()
        return base, expiry, int(strike), opt_type

    # Try 5-digit match (Fallback for weekly)
    m = re.search(r'^([A-Z]+?)([0-9]{5})(\d+)(CE|PE)$', s)
    if m:
        base, expiry, strike, opt_type = m.groups()
        return base, expiry, int(strike), opt_type
        
    return None

def update_auto_strike(idx_ltp: float, idx_name: str, signal: str = None, opt_type_override: str = None):
    """Calculates ATM strike, applies offset, and updates UI symbol."""
    global last_logged_strike
    
    is_base_index = cur_sym in ["NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "BANKEX", "NIFTY_50"] if 'cur_sym' in locals() else False
    # Re-fetch cur_sym to be safe
    cur_sym = tr_symbol.get().strip().upper()
    is_base_index = cur_sym in ["NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "BANKEX", "NIFTY_50"]

    if not auto_strike_var.get(): return
    if buy_active and not is_base_index:
        return # Skip if already in an active option trade
        
    cur_sym = tr_symbol.get().strip().upper()
    if not cur_sym: return
    # print(f"DEBUG: Current Symbol in UI: '{cur_sym}'")
    
    # 1. Detect Strike Step
    strike_step = detect_strike_step(cur_sym)
        
    # 2. Calculate ATM
    atm = round(idx_ltp / strike_step) * strike_step
    
    # 3. Parse Offset
    offset_str = strike_offset_var.get().replace(" ", "") # e.g. "ATM -1" -> "ATM-1"
    offset_val = 0
    if "+" in offset_str:
        try: offset_val = int(offset_str.split("+")[1])
        except: offset_val = 0
    elif "-" in offset_str:
        try: offset_val = -int(offset_str.split("-")[1])
        except: offset_val = 0
    
    # 4. Handle Index-to-Option Bootstrapping
    if cur_sym in ["NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "BANKEX", "NIFTY_50"]:
        # Use the manual CE/PE selector from UI if provided
        opt_type_val = opt_type_override if opt_type_override in ["CE", "PE"] else None
        
        # If not provided via parameter, use signal as fallback
        if not opt_type_val:
            opt_type_val = "CE"
            if signal and "BEARISH" in signal: 
                opt_type_val = "PE"
        
        # Apply offset based on option type
        # For CE: ATM+1 = strike above ATM (OTM for calls)
        # For PE: ATM+1 = strike below ATM (OTM for puts)
        if opt_type_val == "CE":
            final_strike = int(atm + (offset_val * strike_step))
        else:  # PE
            final_strike = int(atm - (offset_val * strike_step))
        
        expiry_str = get_next_expiry(cur_sym.replace('NIFTY_50', 'NIFTY'))
        new_symbol = f"{cur_sym.replace('NIFTY_50', 'NIFTY')}{expiry_str}{final_strike}{opt_type_val}"
        print(f"DEBUG: Bootstrapping Index {cur_sym} -> Full Symbol: {new_symbol} (ATM: {atm}, Offset: {offset_val}, Type: {opt_type_val})")
        
        # Update both symbol and opt_type UI variable
        def update_ui():
            tr_symbol.set(new_symbol)
            try:
                opt_type.set(opt_type_val)
            except:
                pass  # opt_type might not be initialized yet
            print(f"DEBUG: Applied tr_symbol.set({new_symbol})")
        
        root.after(0, update_ui)
        # Only log if strike changed
        if last_logged_strike != final_strike:
            log_with_callback(log_cb, f"🎯 Dynamic Load: {cur_sym} -> {new_symbol} (@{idx_ltp})")
            last_logged_strike = final_strike
        return

    # 5. Check if we actually need to change an existing option symbol's strike
    # Match format: BASE + 5-or-6-digit EXPIRY + STRIKE + CE/PE
    # Example: SENSEX2621284500CE -> expiry is 26212(5) or 260217(6)
    parts = parse_symbol_parts(cur_sym)
    if parts:
        base, expiry, cur_strike, cur_opt_type = parts
        
        # Apply offset based on option type
        if cur_opt_type == "CE":
            final_strike = int(atm + (offset_val * strike_step))
        else:  # PE
            final_strike = int(atm - (offset_val * strike_step))
        
        if cur_strike != final_strike:
            new_symbol = update_symbol_strike(cur_sym, final_strike)
            # print(f"\n⚡ Auto-Strike: Switching {cur_strike} -> {final_strike} for {new_symbol} (Type: {cur_opt_type})")
            root.after(0, lambda: tr_symbol.set(new_symbol))
            # Only log if strike changed
            if last_logged_strike != final_strike:
                log_with_callback(log_cb, f"⚡ Auto-Strike: {final_strike} (@{idx_ltp})")
                last_logged_strike = final_strike


def update_symbol_strike(symbol: str, new_strike: int) -> str:
    """Updates the strike price in an option symbol while preserving expiry.
    Format: NIFTY2621725950CE -> NIFTY26217{NEW_STRIKE}CE
    """
    s = symbol.strip().upper()
    
    # Match: BASE + 5-or-6-digit EXPIRY + STRIKE + CE/PE
    parts = parse_symbol_parts(s)
    if parts:
        base, expiry, old_strike, opt_type = parts
        return f"{base}{expiry}{new_strike}{opt_type}"
    
    # Fallback for symbols without expiry (legacy format)
    m = re.search(r"(\d+)(CE|PE)?$", s)
    if not m:
        return s
    start, end = m.span(1)
    return s[:start] + str(new_strike) + s[end:]


def update_symbol_option(symbol: str, new_type: str) -> str:
    s = symbol.strip().upper()
    if s.endswith("CE") or s.endswith("PE"):
        return s[:-2] + new_type
    return s + new_type

def update_params_by_symbol(*args):
    """Updates Target, SL, TSL, and PT based on the index of the symbol."""
    global last_logged_params_symbol
    
    cur_sym = tr_symbol.get().strip().upper()
    if not cur_sym: return
    
    config = None
    if "NIFTY" in cur_sym:
        config = NIFTY_CONFIG
    elif "SENSEX" in cur_sym or "BSX" in cur_sym:
        config = SENSEX_CONFIG
        
    if config:
        target_var.set(str(config["target"]))
        sl_var.set(str(config["sl"]))
        trail_var.set(str(config["tsl"]))
        pt_var.set(str(config["pt"]))
        # Only log if symbol changed (to avoid spam when strike updates)
        if last_logged_params_symbol != cur_sym:
            log_with_callback(log_cb, f"⚙️ Params updated for {cur_sym}")
            last_logged_params_symbol = cur_sym


# Symbol history removed - using static list instead

# 1️⃣ STRIKE UNIT (Row 0)
ttk.Label(frm, text="Strike:").grid(row=0, column=0, sticky="e", pady=5, padx=5)

def move_strike(step):
    cur_sym = tr_symbol.get().strip().upper()
    if not cur_sym: return
    # Match format: BASE + 5-or-6-digit EXPIRY + STRIKE + CE/PE
    parts = parse_symbol_parts(cur_sym)
    if not parts: return
    base, expiry, cur_strike, opt_type = parts
    strike_step = int(detect_strike_step(cur_sym))
    new_strike = cur_strike + (strike_step * step)
    if new_strike <= 0: return
    new_symbol = update_symbol_strike(cur_sym, new_strike)
    tr_symbol.set(new_symbol)
    log_with_callback(log_cb, f"Updated Strike: {new_strike}")

updown = ttk.Frame(frm)
updown.grid(row=0, column=1, columnspan=5, sticky="w", pady=5)
ttk.Button(updown, text="▲", width=3, command=lambda: move_strike(1)).pack(side="left", padx=1)
ttk.Button(updown, text="▼", width=3, command=lambda: move_strike(-1)).pack(side="left", padx=1)

auto_strike_var = tk.BooleanVar(value=True)
ttk.Checkbutton(updown, text="Auto", variable=auto_strike_var).pack(side="left", padx=(10, 2))

strike_offset_var = tk.StringVar(value="ATM")
offset_combo = ttk.Combobox(updown, textvariable=strike_offset_var, 
                            values=[f"ATM {i:+d}" if i != 0 else "ATM" for i in range(-10, 11)], 
                            width=8, state="readonly")
offset_combo.pack(side="left", padx=2)

def on_offset_change(*args):
    if auto_strike_var.get() and last_idx_ltp > 0:
        # Trigger an immediate update since the user changed the offset
        try:
            current_sig = scalp_manager.get_signal().get('signal')
            current_opt_type = opt_type.get() if 'opt_type' in globals() else None
            update_auto_strike(last_idx_ltp, last_idx_name, signal=current_sig, opt_type_override=current_opt_type)
        except Exception as e:
            print(f"DEBUG: Error in immediate offset update: {e}")

strike_offset_var.trace_add("write", on_offset_change)

ttk.Button(updown, text="RESET", width=8, command=lambda: trigger_override()).pack(side="left", padx=(20, 2))

# 2️⃣ OPTION TYPE (Row 1)
ttk.Label(frm, text="Option:").grid(row=1, column=0, sticky="e", pady=5, padx=5)

def toggle_cepe():
    new = "PE" if opt_type.get() == "CE" else "CE"
    opt_type.set(new)
    cepe_btn.config(text=new, style="CE.TButton" if new == "CE" else "PE.TButton")
    cur = tr_symbol.get().strip()
    if cur:
        tr_symbol.set(update_symbol_option(cur, new))
        log_with_callback(log_cb, f"Updated Option: {new}")
    
    # If auto-strike is enabled, trigger immediate update with new option type
    if auto_strike_var.get() and last_idx_ltp > 0:
        try:
            current_sig = scalp_manager.get_signal().get('signal')
            update_auto_strike(last_idx_ltp, last_idx_name, signal=current_sig, opt_type_override=new)
        except Exception as e:
            print(f"DEBUG: Error in CE/PE toggle update: {e}")

opt_type = tk.StringVar(value="CE")
cepe_btn = ttk.Button(frm, text="CE", width=12, style="CE.TButton", command=toggle_cepe)
cepe_btn.grid(row=1, column=1, sticky="w", pady=5)

# 3️⃣ TRADE PARAMS (Row 2)
ttk.Label(frm, text="Params:").grid(row=2, column=0, sticky="e", pady=5, padx=5)
param_frame = ttk.Frame(frm)
param_frame.grid(row=2, column=1, columnspan=5, sticky="w")

lots_var = tk.StringVar(value="1")
ttk.Combobox(param_frame, textvariable=lots_var, values=["1", "2", "3", "5", "10"], width=4, state="readonly").pack(side="left", padx=2)

ttk.Label(param_frame, text="Tgt:").pack(side="left", padx=(10, 2))
target_var = tk.StringVar(value=str(DEFAULT_TARGET))
ttk.Entry(param_frame, textvariable=target_var, width=5).pack(side="left", padx=2)

ttk.Label(param_frame, text="SL:").pack(side="left", padx=(10, 2))
sl_var = tk.StringVar(value=str(DEFAULT_SL))
ttk.Entry(param_frame, textvariable=sl_var, width=5).pack(side="left", padx=2)

ttk.Label(param_frame, text="Trl:").pack(side="left", padx=(10, 2))
trail_var = tk.StringVar(value=str(DEFAULT_TSL_STEP))
ttk.Entry(param_frame, textvariable=trail_var, width=5).pack(side="left", padx=2)

ttk.Label(param_frame, text="PT:").pack(side="left", padx=(10, 2))
pt_var = tk.StringVar(value=str(DEFAULT_TP_TSL))
ttk.Entry(param_frame, textvariable=pt_var, width=5).pack(side="left", padx=2)

# 4️⃣ SYMBOL SELECTION (Row 3)
ttk.Label(frm, text="Symbol:").grid(row=3, column=0, sticky="e", pady=5, padx=5)
tr_symbol = tk.StringVar(value=DEFAULT_TRADING_SYMBOL)
tr_symbol.trace_add("write", update_params_by_symbol)
symbol_combo = ttk.Combobox(frm, textvariable=tr_symbol, values=["NIFTY", "SENSEX"], width=32, state="readonly")
symbol_combo.grid(row=3, column=1, columnspan=5, sticky="w", pady=5)

# 5️⃣ CONTROL BUTTONS (Row 4)
btn_frame = ttk.Frame(frm)
btn_frame.grid(row=4, column=0, columnspan=6, pady=(10, 2))

buy_btn = ttk.Button(
    btn_frame, text="BUY", style="Buy.TButton", width=12,
    command=lambda: run_bg(do_buy, trade_type="Manual")
)
buy_btn.pack(side="left", padx=10)

ttk.Button(
    btn_frame, text="EXIT", style="Exit.TButton", width=12,
    command=lambda: run_bg(do_exit)
).pack(side="left", padx=10)


auto_buy_btn = ttk.Button(
    btn_frame, text="AUTO BUY: OFF", width=15, style="Auto.TButton",
    command=lambda: toggle_auto_buy()
)
auto_buy_btn.pack(side="left", padx=5)

auto_sell_btn = ttk.Button(
    btn_frame, text="AUTO SELL: OFF", width=15, style="Auto.TButton",
    command=lambda: toggle_auto_sell()
)
auto_sell_btn.pack(side="left", padx=5)

# 5.5️⃣ STATUS BAR (Row 5)
status_frame = ttk.Frame(frm)
status_frame.grid(row=5, column=0, columnspan=6, pady=(2, 5), sticky="ew")

status_label = ttk.Label(status_frame, text="Buy Enabled", font=("Segoe UI", 10))
status_label.pack(side="left", padx=(5, 0))

position_label = ttk.Label(status_frame, text="● NO POSITION", font=("Segoe UI", 10, "bold"), foreground="gray")
position_label.pack(side="left", padx=(20, 0))

strategy_status_label = ttk.Label(status_frame, text="Strategy: Waiting...", font=("Segoe UI", 10, "bold"))
strategy_status_label.pack(side="right", padx=(0, 5))

# High Momentum Alert Label
momentum_alert_label = tk.Label(status_frame, text="", font=("Segoe UI", 10, "bold"), bg=root.cget('bg'))
momentum_alert_label.pack(side="right", padx=(0, 20))

# ---------------------------------------------------------
# LOGIC
# ---------------------------------------------------------


def is_buy_disabled():

    if os.path.exists(BUY_DISABLED_FILE):
        try:
            with open(BUY_DISABLED_FILE, 'r') as f:
                data = json.load(f)
            
            # Reset daily: If the saved date is not today, the lockout is expired.
            saved_date = data.get("date")
            today = datetime.now().strftime("%Y-%m-%d")
            if saved_date and saved_date != today:
                return False, 0, None

            remaining = data["disabled_until"] - time.time()
            if remaining > 0:
                return True, remaining, data.get("last_trade_id")
        except:
            pass
    return False, 0, None

def trigger_override():
    global override_until
    
    # Check if strict max-loss lockout is active
    if os.path.exists(BUY_DISABLED_FILE):
        try:
            with open(BUY_DISABLED_FILE, 'r') as f:
                data = json.load(f)
            reason = data.get("last_trade_id")
            rem = data.get("disabled_until", 0) - time.time()
            # If it's a 24-hour lockout (Hard Stop)
            if reason == "max_loss" or (reason and str(reason).startswith("max_loss") and rem > 80000):
                today = datetime.now().strftime("%Y-%m-%d")
                if data.get("date") == today:
                    log_with_callback(log_cb, "⛔ HARD STOP: Max Loss limit reached. Cannot override until tomorrow.")
                    messagebox.showerror("Hard Stop", "Max Loss limit reached. Trading is disabled for the rest of the day.")
                    return
        except Exception as e:
            log_with_callback(log_cb, f"DEBUG: Error checking override: {e}")

    if os.path.exists(BUY_DISABLED_FILE):
        try:
            # We want to clear the timer, but KEEP the memory
            # of highest_threshold AND last_trade_id so we don't re-lock.
            with open(BUY_DISABLED_FILE, 'r') as f:
                data = json.load(f)
            
            data["disabled_until"] = 0
            # Keeping data["last_trade_id"] as is prevents 3-loss re-trigger
            # Keeping data["highest_threshold"] prevents dollar re-trigger
            
            with open(BUY_DISABLED_FILE, 'w') as f:
                json.dump(data, f)
                
            log_with_callback(log_cb, "✅ Manual RESET: timer cleared. (Memory preserved).")
        except: 
            try: os.remove(BUY_DISABLED_FILE)
            except: pass
    else:
        log_with_callback(log_cb, "✅ Manual RESET: Buy enabled.")
    update_status_label()

def toggle_auto_buy():
    global auto_buy
    auto_buy = not auto_buy
    if auto_buy:
        auto_buy_btn.config(text="AUTO BUY: ON", style="AutoOn.TButton")
        log_with_callback(log_cb, "🤖 Auto Buy: ENABLED")
    else:
        auto_buy_btn.config(text="AUTO BUY: OFF", style="Auto.TButton")
        log_with_callback(log_cb, "⭕ Auto Buy: DISABLED")

def toggle_auto_sell():
    global auto_sell
    auto_sell = not auto_sell
    if auto_sell:
        auto_sell_btn.config(text="AUTO SELL: ON", style="AutoOn.TButton")
        log_with_callback(log_cb, "🤖 Auto Sell: ENABLED")
    else:
        auto_sell_btn.config(text="AUTO SELL: OFF", style="Auto.TButton")
        log_with_callback(log_cb, "⭕ Auto Sell: DISABLED")


def update_datetime_label():
    now_str = datetime.now().strftime("%d-%b-%Y %I:%M:%S %p")
    date_time_label.config(text=now_str)
    root.after(1000, update_datetime_label)


def update_status_label():
    
    disabled, remaining, reason = is_buy_disabled()
    if disabled:
        buy_btn.config(state="disabled")
        buy_btn.config(style="Locked.TButton")
        if reason.startswith("max_loss"):
            # A "Hard Stop" is defined as a lockout of 24 hours (1440 mins) or more
            # We use 86000 as a buffer for 86400 seconds (24h)
            is_static_hard_stop = (reason == "max_loss")
            is_dynamic_hard_stop = (remaining > 80000) 
            
            if is_static_hard_stop or is_dynamic_hard_stop:
                status_label.config(text="Buy Disabled - Max Loss (Hard Stop)", foreground="red")
            else:
                mins = int(remaining // 60)
                secs = int(remaining % 60)
                status_label.config(text=f"Buy Disabled - Loss Limit ({mins}:{secs:02d})", foreground="red")
        elif reason == "max_profit":
            status_label.config(text="Buy Disabled - Max Profit Reached", foreground="green")
        elif reason == "max_loss_count":
            status_label.config(text=f"Buy Disabled - Max Losses ({MAX_DAILY_LOSS_COUNT})", foreground="red")
        else:
            mins = int(remaining // 60)
            secs = int(remaining % 60)
            status_label.config(text=f"Buy Disabled - Re-enables in {mins}:{secs:02d}", foreground="red")
    else:
        # Check Cool-off period
        time_since_exit = time.time() - last_exit_time
        if time_since_exit < COOL_OFF_PERIOD:
            remaining_cool = int(COOL_OFF_PERIOD - time_since_exit)
            buy_btn.config(state="disabled")
            status_label.config(text=f"Cooling Off... ({remaining_cool}s)", foreground="#f59e0b")
        else:
            if not buy_active:
                buy_btn.config(state="normal")
                if market_sideways:
                    buy_btn.config(style="Sideways.TButton")
                    status_label.config(text="Market Sideways (Caution)", foreground="#f59e0b")
                else:
                    buy_btn.config(style="Buy.TButton")
                    status_label.config(text="Buy Enabled", foreground="green")
            else:
                # buy_active is True
                buy_btn.config(state="disabled")
                buy_btn.config(style="Buy.TButton")
                status_label.config(text="Position Active", foreground="green")
    root.after(1000, update_status_label)


def do_buy(trade_type="Manual"):
    global buy_active, buy_pending, active_symbol, last_buy_price, max_price_seen, active_trade_metadata, last_exit_time
    active_trade_metadata = {} # Reset at start to avoid stale data from previous trades

    disabled, remaining, reason = is_buy_disabled()
    if disabled:
        if reason.startswith("max_loss"):
            if remaining > 80000 or reason == "max_loss":
                msg = f"Buy disabled. Net loss has reached the daily hard stop limit."
            else:
                mins = int(remaining // 60)
                secs = int(remaining % 60)
                msg = f"Buy disabled due to loss reaching a progressive threshold. Re-enables in {mins} minutes {secs} seconds."
        elif reason == "max_profit":
            msg = f"Buy disabled. Net profit has exceeded the limit of {BUY_DISABLE_MAX_PROFIT}."
        else:
            mins = int(remaining // 60)
            secs = int(remaining % 60)
            msg = f"Buy disabled due to {BUY_DISABLE_LOSS_COUNT} continuous losses. Re-enables in {mins} minutes {secs} seconds."
        
        root.after(0, lambda: messagebox.showerror("Buy Disabled", msg))
        return

    if buy_active or buy_pending:
        log_with_callback(log_cb, f"BUY blocked: {'Pending' if buy_pending else 'Position'} already active")
        return

    # Check Cool-off Period
    time_since_exit = time.time() - last_exit_time
    if time_since_exit < COOL_OFF_PERIOD:
        remaining = int(COOL_OFF_PERIOD - time_since_exit)
        log_with_callback(log_cb, f"BUY blocked: Cool-off period active ({remaining}s remaining)")
        return

    tr = tr_symbol.get().strip().upper()
    if not tr:
        messagebox.showerror("Error", "Trading symbol is empty")
        return

    # 🔒 Lock entering phase
    buy_pending = True
    active_symbol = tr

    # Double check with API for open positions
    try:
        client = get_client()
        if client:
            pos_resp = client.positions()
            data = pos_resp.get("data", []) if isinstance(pos_resp, dict) else pos_resp
            if isinstance(data, list):
                for p in data:
                    if int(float(p.get("netQty", p.get("flQty", 0)))) != 0:
                        log_with_callback(log_cb, f"BUY blocked: API shows open position in {p.get('trdSym')}")
                        buy_active = True
                        return
    except Exception as e:
        log_with_callback(log_cb, f"⚠️ Position Check Error: {e}")

    tr = tr_symbol.get().strip()
    if not tr:
        messagebox.showerror("Error", "Trading symbol is empty")
        return

    # Safe UI update via root.after or immediate if called in way that allows it
    # Since do_buy is in bg, we use root.after for widget config
    root.after(0, lambda: buy_btn.config(state="disabled"))  # 🔒 disable immediately

    # Ensure client is available
    client = get_client()
    token = find_token_for_trading_symbol(tr, log_cb)
    
    # Capture current LTP for target tracking (Initial estimate)
    try:
        if client:
            exch = detect_exchange_segment(tr)
            q = client.quotes(instrument_tokens=[{"instrument_token": token, "exchange_segment": exch}], quote_type="ltp")
            ltp = 0
            if isinstance(q, list) and len(q) > 0: 
                ltp = float(q[0].get("ltp", 0))
            elif isinstance(q, dict): 
                data = q.get("data", [])
                if isinstance(data, list) and len(data) > 0:
                    ltp = float(data[0].get("ltp", 0))
            
            if ltp > 0:
                last_buy_price = ltp
                max_price_seen = ltp
                log_with_callback(log_cb, f"Initial LTP Captured: {last_buy_price}")
            else:
                log_with_callback(log_cb, "⚠️ Warning: Initial LTP capture returned 0")
    except Exception as e:
        log_with_callback(log_cb, f"⚠️ LTP Fetch Error: {e}")

    # Place order and capture response
    try:
        resp = place_market_order(token, int(lots_var.get()), "BUY", tr, log_cb)
    except Exception as e:
        log_with_callback(log_cb, f"❌ BUY ORDER ERROR: {e}")
        buy_pending = False
        active_symbol = ""
        root.after(0, lambda: buy_btn.config(state="normal"))
        return

    # 🎯 Verify if order was actually placed
    if not (isinstance(resp, dict) and "nOrdNo" in resp):
        log_with_callback(log_cb, f"❌ BUY FAILED: {resp.get('errMsg', 'Unknown Error') if isinstance(resp, dict) else 'Invalid Response'}")
        buy_pending = False
        active_symbol = ""
        root.after(0, lambda: buy_btn.config(state="normal"))
        return

    order_id = resp["nOrdNo"]

    # 🎯 Verify if order was actually COMPLETED
    is_completed = False
    status = "unknown"
    try:
        if client:
            log_with_callback(log_cb, f"Verifying completion for Order: {order_id}...")
            
            # Retry loop for order status (API can be slow)
            for attempt in range(6): # 6 attempts * 0.5s = 3s total
                time.sleep(0.5) 
                history_resp = client.order_history(order_id=order_id)
                # print(f"DEBUG: Order History Response (BUY) for {order_id}: {history_resp}")
                
                # Handle both list and dict-with-data formats
                history = history_resp if isinstance(history_resp, list) else history_resp.get("data", []) if isinstance(history_resp, dict) else []
                
                if history and isinstance(history, list):
                    latest_state = history[-1]
                    status = latest_state.get("ordSt", "").lower()
                    
                    if status == "complete":
                        is_completed = True
                        for state in reversed(history):
                            # Check multiple possible keys for average price
                            avg_p = state.get("avgPrc") or state.get("buyAvgPrc") or state.get("price")
                            if avg_p and float(avg_p) > 0:
                                last_buy_price = float(avg_p)
                                max_price_seen = last_buy_price
                                break
                        break # Success!
                    elif status == "rejected":
                        reason = latest_state.get("rejReason", "Unknown Reason")
                        log_with_callback(log_cb, f"❌ BUY REJECTED: {reason}")
                        break
                    else:
                        # Continue retrying if status is not final
                        pass
                
            if not is_completed and status != "rejected":
                # Fallback: Check Order Report for this ID
                try:
                    rep = client.order_report()
                    rep_data = rep.get("data", []) if isinstance(rep, dict) else rep
                    if isinstance(rep_data, list):
                        for o in rep_data:
                            if str(o.get("nOrdNo")) == str(order_id) and str(o.get("ordSt", "")).lower() == "complete":
                                is_completed = True
                                avg_p = o.get("avgPrc") or o.get("buyAvgPrc") or o.get("price")
                                if avg_p: last_buy_price = float(avg_p)
                                break
                except: pass
                
            if not is_completed and status != "rejected":
                log_with_callback(log_cb, f"⚠️ Verification Timeout: Order {order_id} status is still {status}")
                
    except Exception as e:
        log_with_callback(log_cb, f"Verification Error: {e}")

    if not is_completed:
        # Check if we can recover from positions even if verification timed out
        try:
            pos_resp = client.positions()
            data = pos_resp.get("data", []) if isinstance(pos_resp, dict) else pos_resp
            if isinstance(data, list):
                for p in data:
                    if p.get("trdSym", "").upper() == active_symbol.upper() and int(float(p.get("netQty", 0))) != 0:
                        log_with_callback(log_cb, "🛡️ RECOVERED: Order verification timed out, but Position is open in API. Resuming...")
                        is_completed = True
                        # Check multiple keys in position data too
                        recovered_p = p.get("avgPrc") or p.get("buyAvgPrc") or p.get("buyAvg")
                        if recovered_p and float(recovered_p) > 0:
                            last_buy_price = float(recovered_p)
                        break
        except: pass

    if not is_completed:
        buy_pending = False
        active_symbol = ""
        root.after(0, lambda: buy_btn.config(state="normal"))
        return

    buy_active = True  
    buy_pending = False # Done
    
    # Capture Entry Indicators
    try:
        ind = scalp_manager.get_signal()
        active_trade_metadata = {
            "symbol": active_symbol, # Track which symbol this metadata belongs to
            "trade_type": trade_type,
            "entry_sig": ind.get("signal"),
            "entry_ema": ind.get("ema"),
            "entry_roc": ind.get("roc"),
            "entry_bbw": ind.get("bb_width"),
            "entry_time_ts": time.time(), # For time-stop
            "exit_reason": "IN_PROGRESS"
        }
    except: active_trade_metadata = {}

    tgt_pts = float(target_var.get() or 0)
    sl_pts = float(sl_var.get() or 0)
    tgt_price = round(last_buy_price + tgt_pts, 2)
    sl_price = round(last_buy_price - sl_pts, 2)

    log_with_callback(log_cb, f"🎯 Confirmed BUY Executed @ {last_buy_price}") 
    # Telegram Notification Removed per User Request

    log_with_callback(log_cb, f"📊 Target: {tgt_price} (+{tgt_pts}) | SL: {sl_price} (-{sl_pts})")
    log_with_callback(log_cb, f"✅ BUY completed - waiting for SELL")


def do_exit(reason="Manual"):
    global buy_active, exit_pending, active_symbol, active_trade_metadata

    # Use the symbol we actually BOUGHT, not whatever is currently in the entry box
    tr = active_symbol if active_symbol else tr_symbol.get().strip().upper()
    
    if not tr:
        log_with_callback(log_cb, "❌ EXIT Error: No active symbol tracked.")
        return
    
    if exit_pending:
        log_with_callback(log_cb, "⌛ EXIT blocked: Exit order already pending")
        return
    
    exit_pending = True
    # Capture Exit Indicators before placing order
    try:
        ind = scalp_manager.get_signal()
        active_trade_metadata.update({
            "exit_sig": ind.get("signal"),
            "exit_ema": ind.get("ema"),
            "exit_roc": ind.get("roc"),
            "exit_bbw": ind.get("bb_width"),
            "exit_reason": reason
        })
    except: pass

    token = find_token_for_trading_symbol(tr, log_cb)
    try:
        resp = place_market_order(token, int(lots_var.get()), "SELL", tr, log_cb)
    except Exception as e:
        log_with_callback(log_cb, f"❌ SELL ORDER ERROR: {e}")
        exit_pending = False
        return

    # 🎯 Verify if order was actually placed
    if not (isinstance(resp, dict) and "nOrdNo" in resp):
        log_with_callback(log_cb, f"❌ SELL FAILED: {resp.get('errMsg', 'Unknown Error') if isinstance(resp, dict) else 'Invalid Response'}")
        exit_pending = False
        return

    order_id = resp["nOrdNo"]

    # Capture Execution Price for SELL and Verify Completion
    sell_price = 0.0
    is_completed = False
    status = "unknown"
    try:
        client = get_client()
        if client:
            log_with_callback(log_cb, f"Verifying completion for SELL Order: {order_id}...")
            # Retry loop for order status (API can be slow)
            for attempt in range(6): # 6 attempts * 0.5s = 3s total
                time.sleep(0.5) 
                history_resp = client.order_history(order_id=order_id)
                # print(f"DEBUG: Order History Response (SELL) for {order_id}: {history_resp}")
                
                # Handle both list and dict-with-data formats
                history = history_resp if isinstance(history_resp, list) else history_resp.get("data", []) if isinstance(history_resp, dict) else []
                
                if history and isinstance(history, list):
                    latest_state = history[-1]
                    status = latest_state.get("ordSt", "").lower()

                    if status == "complete":
                        is_completed = True
                        for state in reversed(history):
                            # Check multiple possible keys for average price
                            avg_p = state.get("avgPrc") or state.get("sellAvgPrc") or state.get("price")
                            if avg_p and float(avg_p) > 0:
                                sell_price = float(avg_p)
                                break
                        break 
                    elif status == "rejected":
                        reason = latest_state.get("rejReason", "Unknown Reason")
                        log_with_callback(log_cb, f"❌ SELL REJECTED: {reason}")
                        break
            
            if not is_completed and status != "rejected":
                # Fallback: Check Order Report for this ID
                try:
                    rep = client.order_report()
                    rep_data = rep.get("data", []) if isinstance(rep, dict) else rep
                    if isinstance(rep_data, list):
                        for o in rep_data:
                            if str(o.get("nOrdNo")) == str(order_id) and str(o.get("ordSt", "")).lower() == "complete":
                                is_completed = True
                                avg_p = o.get("avgPrc") or o.get("sellAvgPrc") or o.get("price")
                                if avg_p: sell_price = float(avg_p)
                                break
                except: pass

            if not is_completed and status != "rejected":
                log_with_callback(log_cb, f"⚠️ SELL Timeout: Order {order_id} status is still {status}")

    except Exception as e:
        log_with_callback(log_cb, f"SELL Verification Error: {e}")

    if not is_completed:
        exit_pending = False # Reset so we can try again
        return

    buy_active = False               
    exit_pending = False
    active_symbol = ""
    global last_exit_time
    last_exit_time = time.time() # Start cool-off period
    root.after(0, lambda: buy_btn.config(state="normal"))
    
    log_msg = f"🎯 Confirmed SELL Executed @ {sell_price if sell_price > 0 else 'MARKET'}"
    if reason != "Manual":
        log_msg += f" [{reason}]"
    log_with_callback(log_cb, log_msg)
    # Telegram Notification Removed per User Request
    
    log_with_callback(log_cb, "✅ POSITION CLOSED")


# ---------------------------------------------------------
# MONITOR LOGIC
# ---------------------------------------------------------

def color_pnl(val):
    if val > 0: return "green"
    if val < 0: return "#ff4444"
    return "white"

def update_monitor_ui():
    """Background worker to fetch and process PnL data."""
    global pnl_engine, active_symbol, last_buy_price, max_price_seen, buy_active, last_trade_count, active_trade_metadata, last_mom_alert_time, mom_threshold_start
    try:
        client = get_client()
        if not client:
            return

        # 1. POSITION SYNC (Source of Truth)
        try:
            pos_resp = client.positions()
            data = pos_resp.get("data", []) if isinstance(pos_resp, dict) else pos_resp
            has_open_pos = False
            if isinstance(data, list):
                # print(f"DEBUG: Raw Position Response: {data[0]}")
                for p in data:
                    # 🛡️ BSE FO Fallback: If netQty is missing, calculate from fills
                    fl_buy = float(p.get("flBuyQty", 0))
                    fl_sell = float(p.get("flSellQty", 0))
                    qty = int(float(p.get("netQty", fl_buy - fl_sell)))
                    
                    if qty != 0:
                        # Only count as 'buy_active' if it's a relevant Index/FO position
                        sym_p = p.get("trdSym", "").upper()
                        exch_p = p.get("exch", "").lower()
                        
                        # Exclude equity positions (ending with -EQ)
                        if "-EQ" in sym_p:
                            continue
                        
                        # Only count FO positions or index derivatives
                        if "fo" in exch_p or any(x in sym_p for x in ["NIFTY", "SENSEX", "BANKNIFTY", "FINNIFTY", "BANKEX"]):
                            # Additional check: must not be an equity ETF
                            if "BEES" not in sym_p and "ETF" not in sym_p:
                                has_open_pos = True
                                print(f"DEBUG: Active FO Position found: {sym_p} ({qty})")
                        # 🛡️ AUTO-RECOVERY from positions API (Source of Truth)
                        if not active_symbol or last_buy_price <= 0:
                             active_symbol = p.get("trdSym", "").upper()
                             # Check multiple possible keys for average price in positions
                             recovered_p = p.get("avgPrc") or p.get("buyAvgPrc") or p.get("buyAvg")
                             
                             # BSE Fallback: Price = BuyAmt / flBuyQty
                             if not recovered_p or float(recovered_p) == 0:
                                 buy_amt = float(p.get("buyAmt", 0))
                                 if fl_buy > 0: recovered_p = buy_amt / fl_buy
                             
                             last_buy_price = float(recovered_p or 0)
                             if last_buy_price > 0:
                                log_with_callback(log_cb, f"🔄 Recovered Position from API: {active_symbol} @ {last_buy_price}")
                        break
            
            buy_active = has_open_pos
            
            # Update UI Indicator
            if buy_active:
                root.after(0, lambda: position_label.config(text="● POSITION OPEN", foreground="#ef4444"))
            else:
                root.after(0, lambda: position_label.config(text="● NO POSITION", foreground="#6b7280"))
        except Exception as pe:
             log_with_callback(log_cb, f"⚠️ Position Sync Error: {pe}")
        
        # 2. ORDER PROCESSING & STATS
        report = client.order_report()
        # Handle both list and dict-with-data formats
        report_data = report.get("data", []) if isinstance(report, dict) else report
        
        if report_data and isinstance(report_data, list):
            # Refreshed engine with latest data
            new_engine = PositionPnLEngine(initial_capital=pnl_engine.initial_capital)
            trades = parse_api_orders(report_data)
            trades.sort(key=lambda x: x.time)
            for t in trades:
                new_engine.add_trade(t)
            # Enrich completed trades
            if len(new_engine.completed_trades) > last_trade_count:
                for i in range(last_trade_count, len(new_engine.completed_trades)):
                    trade = new_engine.completed_trades[i]
                    if active_trade_metadata:
                        # Only clear/apply metadata if it belongs to THIS symbol
                        if trade['symbol'] == active_trade_metadata.get("symbol"):
                            trade.update(active_trade_metadata)
                            active_trade_metadata = {} 
                        elif not trade.get('entry_sig') and not active_symbol:
                            # Fallback cleanup only if no new trade is active
                            trade.update(active_trade_metadata)
                            active_trade_metadata = {}
                
                # Save ONLY the new trades to CSV (Appended)
                new_trades = new_engine.completed_trades[last_trade_count:]
                save_trades_to_csv(new_trades)
                last_trade_count = len(new_engine.completed_trades)
            
            pnl_engine = new_engine
            process_buy_disable_logic(pnl_engine)

        # 2.1 Calculate Stats using persistent engine
        completed = pnl_engine.completed_trades
        weekly_completed, week_start, week_end = filter_current_week_trades(completed)

        pnls = [round(t["net_pnl"]) for t in weekly_completed]
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)
        win_rate = round((wins / len(pnls) * 100), 1) if pnls else 0.0
        
        gross = round(sum(t["gross_pnl"] for t in weekly_completed), 2)
        net = round(sum(t["net_pnl"] for t in weekly_completed), 2)
        charges = round(sum(t["charges"] for t in weekly_completed), 2)

        today = datetime.now().date()
        day_net = round(
            sum(
                t["net_pnl"]
                for t in weekly_completed
                if ((t.get("sell_time") or t.get("buy_time")) and (t.get("sell_time") or t.get("buy_time")).date() == today)
            ),
            2,
        )
        
        last_str = " | ".join(str(p) for p in pnls[-5:])

        # 3. Handle LTP for Indicators (Use Index instead of Option)
        tr_current_ui = tr_symbol.get().strip().upper()
        # Use active_symbol if we have one, otherwise UI symbol
        tr_for_quote = active_symbol if (buy_active and active_symbol) else tr_current_ui
        idx_token, idx_exch, idx_name = get_underlying_index(tr_for_quote)
        
        idx_ltp = 0
        if idx_token:
            try:
                # Fetch Index LTP
                quote_resp = client.quotes(instrument_tokens=[{"instrument_token": idx_token, "exchange_segment": idx_exch}], quote_type="ltp")
                
                if isinstance(quote_resp, list) and len(quote_resp) > 0:
                    idx_ltp = float(quote_resp[0].get("ltp", 0))
                elif isinstance(quote_resp, dict):
                    # Handle both {'data': [...]} and direct dict formats
                    data = quote_resp.get("data", [])
                    if isinstance(data, list) and len(data) > 0:
                        idx_ltp = float(data[0].get("ltp", 0))
                    elif "ltp" in quote_resp:
                        idx_ltp = float(quote_resp.get("ltp", 0))
                
                # Update terminal with live LTP
                # Update terminal with live LTP
                debug_msg = f"DEBUG: UI SYMBOL: {tr_current_ui} | INDEX {idx_name}: {idx_ltp} | ACTIVE: {buy_active}"
                print(f"{debug_msg}{' ' * (80 - len(debug_msg))}", end='\r', flush=True)

                if idx_ltp > 0:
                    global last_idx_ltp, last_idx_name
                    last_idx_ltp = idx_ltp
                    last_idx_name = idx_name
                    
                    scalp_manager.add_ltp(idx_ltp, idx_name)
                    # 🚀 AUTO STRIKE SELECTION
                    current_sig = scalp_manager.get_signal().get('signal')
                    try:
                        current_opt_type = opt_type.get()
                    except:
                        current_opt_type = None  # opt_type not initialized yet
                    update_auto_strike(idx_ltp, idx_name, signal=current_sig, opt_type_override=current_opt_type)
                else:
                    log_with_callback(log_cb, f"⚠️ Warning: Index fetch for {idx_name} returned 0. Using fallback.")
            except Exception as e:
                log_with_callback(log_cb, f"⚠️ Index Quote Error ({idx_name}): {e}")
        
        # Fallback Indicator Data: If index quote failed OR no index mapping, 
        # use the Option LTP to keep the manager "Warm" and moving.
        if not idx_token or idx_ltp <= 0:
            try:
                token = find_token_for_trading_symbol(tr_current_ui)
                if token:
                    exch = detect_exchange_segment(tr_current_ui)
                quote_resp = client.quotes(instrument_tokens=[{"instrument_token": token, "exchange_segment": exch}], quote_type="ltp")
                
                ltp = 0
                if isinstance(quote_resp, list) and len(quote_resp) > 0:
                    ltp = float(quote_resp[0].get("ltp", 0))
                elif isinstance(quote_resp, dict):
                    data = quote_resp.get("data", [])
                    if isinstance(data, list) and len(data) > 0:
                        ltp = float(data[0].get("ltp", 0))
                
                if ltp > 0:
                    scalp_manager.add_ltp(ltp, tr_current_ui)
            except: pass
        
        indicator_data = scalp_manager.get_signal()
        sig = indicator_data['signal']

        # 🎯 CAPITAL CALCULATION
        cur_cap = pnl_engine.get_current_capital()
        pct_pnl = pnl_engine.get_pnl_percentage()

        lines = [
            f"Week (Mon-Fri): {week_start} to {week_end}",
            f"Trades: {len(weekly_completed)}",
            f"W/L   : {wins}/{losses} ({win_rate}%)",
            "-" * 20,
            f"Gross PnL: {gross}",
            f"Net PnL  : {net}",
            f"Day PnL  : {day_net}",
            f"Charges  : {charges}",
            "-" * 20,
            f"RSI: {indicator_data.get('rsi', 0)} | Mom: {indicator_data.get('momentum', 0)}",
            f"Res: {indicator_data.get('sr', {}).get('resistance', 0)} | Sup: {indicator_data.get('sr', {}).get('support', 0)}",
            f"H/L: {indicator_data.get('sr', {}).get('day_high', 0)} / {indicator_data.get('sr', {}).get('day_low', 0)}",
            f"Recent: {last_str}"
        ]
        
        try:
            th = pnl_engine.trading_hours_summary()
            lines.append(f"Avg/hr    : {th['avg_per_hour']}")
        except: pass
        
        global market_sideways, market_exhausted, eod_saved_today
        market_sideways = indicator_data.get("sideways", False)
        market_exhausted = indicator_data.get("exhausted", False)

        # 🎯 EOD LOGIC
        now = datetime.now()
        # Save EOD Capital at 3:30 PM (15:30)
        if now.hour == 15 and now.minute >= 30 and not eod_saved_today:
            save_capital_to_csv(pnl_engine.initial_capital, net, cur_cap, pct_pnl, len(completed))
        elif now.hour < 9: # Reset for new day
            eod_saved_today = False

        if sig == "BULLISH":
            trade_status = "📈 BULLISH SETUP"
            trade_color = "#10b981" # Emerald Green
        elif sig == "BEARISH":
            trade_status = "📉 BEARISH SETUP"
            trade_color = "#10b981" # Emerald Green
        elif market_exhausted:
            trade_status = "⚠️ MARKET EXHAUSTED"
            trade_color = "#ef4444" # Red
        elif market_sideways:
            trade_status = f"⏸️ SIDEWAYS (RSI:{indicator_data.get('rsi')} / Mtm:{indicator_data.get('momentum')})"
            trade_color = "#f97316" # Orange
        elif sig == "NEUTRAL":
            trade_status = f"⚖️ NEUTRAL (RSI:{indicator_data.get('rsi')} / Tgt:{RSIM_RSI_UP}|{RSIM_RSI_DOWN})"
            trade_color = "#6b7280" # Gray
        elif "CONFIRMING" in sig:
            trade_status = f"⏳ {sig}"
            trade_color = "#f97316" # Orange
        elif "WAITING" in sig:
            # Extract (N pts) info
            pts_info = sig.split("WAITING")[1] if "WAITING" in sig else ""
            trade_status = f"⏳ WARMING UP{pts_info}"
            trade_color = "#6b7280" # Gray
        else:
            trade_status = f"🔍 {sig}"
            trade_color = "#ef4444" # Red
            
        # Expansion Pulse Detection Feedback
        if indicator_data.get("pulse") and not market_sideways:
            trade_status = f"⚡ {trade_status} (PULSE DETECTED)"
            trade_color = "#3b82f6" # Bright Blue

        # 🎯 AUTOMATIC TARGET EXIT LOGIC
        if auto_sell and buy_active and not exit_pending:
            cur_ltp = 0
            
            # 🛡️ RECOVER STATE if buy_active is true but we lost the price/symbol
            # (Already recovered from API above, this is a safety fallback)
            if not active_symbol or last_buy_price <= 0:
                for sym, pos_list in pnl_engine.open_trades.items():
                    if pos_list:
                        active_symbol = sym
                        last_buy_price = pos_list[0]['buy_price']
                        log_with_callback(log_cb, f"🔄 Recovered Position from Engine: {active_symbol} @ {last_buy_price}")
                        break

            if last_buy_price <= 0:
                #log_with_callback(log_cb, "⚠️ Warning: Position active but entry price is 0. Exit logic skipped.")
                time.sleep(1)
            else:
                try:
                    # Check price of the symbols we bought
                    tr_to_exit = active_symbol if active_symbol else tr_current_ui
                    option_token = find_token_for_trading_symbol(tr_to_exit)
                    option_exch = detect_exchange_segment(tr_to_exit)
                    
                    q = client.quotes(instrument_tokens=[{"instrument_token": option_token, "exchange_segment": option_exch}], quote_type="ltp")
                    if isinstance(q, list) and len(q) > 0: 
                        cur_ltp = float(q[0].get("ltp", 0))
                    elif isinstance(q, dict): 
                        data = q.get("data", [])
                        if isinstance(data, list) and len(data) > 0:
                            cur_ltp = float(data[0].get("ltp", 0))
                except Exception as qe:
                    log_with_callback(log_cb, f"⚠️ Quote Fetch/Parse Error: {qe}")
            
            if cur_ltp > 0:
                global max_price_seen
                profit = cur_ltp - last_buy_price
                target = float(target_var.get() or 0)
                initial_sl = float(sl_var.get() or 0)
                trail_step = float(trail_var.get() or 0)
                pt_step = float(pt_var.get() or 0)
                
                # Update Max Price for Trailing
                if cur_ltp > max_price_seen:
                    max_price_seen = cur_ltp
                
                # Calculate Effective SL
                # If trail is > 0, we move the SL up by the profit peak amount
                trail_gain = 0
                if trail_step > 0:
                    trail_gain = max_price_seen - last_buy_price
                
                effective_sl_pts = initial_sl - trail_gain
                
                target_price = last_buy_price + target
                sl_price = last_buy_price - effective_sl_pts
                
                lines.append("-" * 20)
                lines.append(f"Position: {profit:+.2f} pts")
                lines.append(f"Tgt : {target_price:.2f} | SL: {sl_price:.2f}")
                
                # if auto_buy or auto_sell:
                #     print(f"DEBUG: Track Exit | {active_symbol} | LTP: {cur_ltp} | Profit: {profit:+.2f} | Tgt: {target} | SL: {-effective_sl_pts:.2f}")

                if auto_sell and profit >= target:
                    if pt_step > 0:
                        max_profit = max_price_seen - last_buy_price
                        pt_trigger_price = (last_buy_price + max_profit) - pt_step
                        if profit <= (max_profit - pt_step):
                            log_with_callback(log_cb, f"🎯 TRAILING PROFIT HIT (Peak {max_profit:+.2f} -> Current {profit:+.2f}). Exiting...")
                            run_bg(do_exit, reason="Trail-Profit")
                            return
                        # Display Trailing Trigger Level
                        lines.append(f"Exit Tgt: {pt_trigger_price:.2f} (Trailing)")
                    else:
                        log_with_callback(log_cb, f"🎯 TARGET REACHED ({profit:+.2f} pts). Exiting...")
                        run_bg(do_exit, reason="Target")
                        return
                elif auto_sell and profit <= -effective_sl_pts:
                    log_with_callback(log_cb, f"🛑 STOP LOSS HIT ({profit:+.2f} pts). Exiting...")
                    run_bg(do_exit, reason="SL")
                    return
        
        lines.append("-" * 20)
        lines.append(f"Source: {idx_name or tr_for_quote}")

        # Update Scalper UI Strategy Label
        root.after(0, lambda: strategy_status_label.config(text=f"Strategy: {trade_status}", foreground=trade_color))

        # ⚡ HIGH MOMENTUM FLASH ALERT
        mom_val = indicator_data.get('momentum', 0)
        if abs(mom_val) >= 20:
            if mom_threshold_start == 0:
                mom_threshold_start = time.time()
            
            # SUSTAIN check
            if time.time() - mom_threshold_start >= 4:
                # Alternating red shades for a "flash" effect when UI refreshes
                flash_color = "#ef4444" if (int(time.time() * 2) % 2 == 0) else "#b91c1c"
                root.after(0, lambda m=mom_val, c=flash_color: momentum_alert_label.config(
                    text=f" 🔥 {m:+.2f} 🔥 ", fg="white", bg=c
                ))
                
                # 📢 TELEGRAM ALERT (1-min cooldown)
                if time.time() - last_mom_alert_time > TELEGRAM_COOL_OFF: 
                    last_mom_alert_time = time.time()
                    direction = "🚀 BULLISH SURGE" if mom_val > 0 else "📉 BEARISH CRASH"
                    alert_msg = f"⚡ **\n\nIndex: {idx_name or tr_for_quote}\nDirection: {direction}\nValue: {mom_val:+.2f}\nLTP: {idx_ltp if idx_ltp > 0 else 'N/A'}"
                    run_bg(send_telegram_msg, alert_msg)
            else:
                root.after(0, lambda: momentum_alert_label.config(text="", bg=root.cget('bg')))
        else:
            mom_threshold_start = 0
            root.after(0, lambda: momentum_alert_label.config(text="", bg=root.cget('bg')))

        # 🤖 AUTO MODE LOGIC
        if auto_buy and not buy_active and not buy_pending:
            tr = tr_symbol.get().strip().upper()
            is_ce = tr.endswith("CE")
            is_pe = tr.endswith("PE")
            
            should_buy = False
            mismatch_msg = ""
            
            if sig == "BULLISH":
                if is_ce: should_buy = True
                elif is_pe: 
                    log_with_callback(log_cb, "🤖 AUTO: Switching to CE for BULLISH signal...")
                    def _switch_to_ce():
                        try:
                            opt_type.set("CE")
                            cepe_btn.config(text="CE", style="CE.TButton")
                            tr_symbol.set(update_symbol_option(tr, "CE"))
                            if auto_strike_var.get():
                                update_auto_strike(last_idx_ltp, last_idx_name, signal="BULLISH", opt_type_override="CE")
                        except: pass
                    root.after(0, _switch_to_ce)
                    return # Wait for next tick to buy
                    
            elif sig == "BEARISH":
                if is_pe: should_buy = True
                elif is_ce: 
                    log_with_callback(log_cb, "🤖 AUTO: Switching to PE for BEARISH signal...")
                    def _switch_to_pe():
                        try:
                            opt_type.set("PE")
                            cepe_btn.config(text="PE", style="PE.TButton")
                            tr_symbol.set(update_symbol_option(tr, "PE"))
                            if auto_strike_var.get():
                                update_auto_strike(last_idx_ltp, last_idx_name, signal="BEARISH", opt_type_override="PE")
                        except: pass
                    root.after(0, _switch_to_pe)
                    return # Wait for next tick to buy

            if should_buy:
                # Check if buy is actually allowed (not disabled)
                disabled, _, reason = is_buy_disabled()
                if not disabled:
                    log_with_callback(log_cb, f"🤖 AUTO: {sig} signal matched with {tr}. Placing BUY...")
                    run_bg(do_buy, trade_type="Auto")
                else:
                    # Log why it didn't trade if disabled
                    pass # Status label already shows this
            elif mismatch_msg:
                # Log the mismatch skip once per signal change to avoid log spam
                # We can use a simple state check if needed, but for now log it.
                log_with_callback(log_cb, f"⚠️ AUTO: {mismatch_msg}")

        # 4. Render on Main Thread

        # 5. Render on Main Thread
        root.after(0, lambda: render_monitor_display(lines, net, gross, day_net))

    except Exception as e:
        log_with_callback(log_cb, f"Monitor Error: {e}")
    finally:
        # Schedule next update correctly
        root.after(REFRESH_INTERVAL_MS, lambda: run_bg(update_monitor_ui))
        
        # Periodically refresh remote config (every 5 minutes)
        if not hasattr(update_monitor_ui, "last_config_refresh"):
            update_monitor_ui.last_config_refresh = 0
            
        if time.time() - update_monitor_ui.last_config_refresh > 300:
            global BUY_DISABLE_MAX_LOSS, PROGRESSIVE_LOSS_CONFIG
            
            # Refresh Max Loss
            new_limit = fetch_remote_config(REMOTE_CONFIG_URL, BUY_DISABLE_MAX_LOSS)
            if new_limit != BUY_DISABLE_MAX_LOSS:
                BUY_DISABLE_MAX_LOSS = new_limit
                log_with_callback(log_cb, f"🔄 Max Loss Limit updated from remote: {BUY_DISABLE_MAX_LOSS}")
            
            # Refresh Progressive Config
            new_prog = fetch_remote_json(PROGRESSIVE_LOSS_URL, PROGRESSIVE_LOSS_CONFIG)
            if new_prog != PROGRESSIVE_LOSS_CONFIG:
                PROGRESSIVE_LOSS_CONFIG = new_prog
                log_with_callback(log_cb, "🔄 Progressive Loss Config updated from remote.")
                
            update_monitor_ui.last_config_refresh = time.time()

def process_buy_disable_logic(engine):
    # 1. Daily Stats
    completed = engine.completed_trades
    net_pnl = sum(t.get("net_pnl", 0) for t in completed)
    loss_amount = -net_pnl
    today_str = datetime.now().strftime("%Y-%m-%d")

    # Load highest threshold hit today to prevent "downgrading" lockouts
    current_label = None
    highest_threshold_hit = 0
    disabled_until_ts = 0
    if os.path.exists(BUY_DISABLED_FILE):
        try:
            with open(BUY_DISABLED_FILE, 'r') as f:
                data = json.load(f)
                if data.get("date") == today_str:
                    current_label = data.get("last_trade_id")
                    highest_threshold_hit = data.get("highest_threshold", 0)
                    disabled_until_ts = data.get("disabled_until", 0)
        except: pass

    # 2. Threshold Check
    # Find the single HIGHEST applicable threshold
    # Sort config High-to-Low to ensure we get the biggest mismatch
    sorted_config = sorted(PROGRESSIVE_LOSS_CONFIG, key=lambda x: x[0], reverse=True)
    app_t, app_d = 0, 0
    for threshold, duration_mins in sorted_config:
        if loss_amount >= threshold:
            app_t = threshold
            app_d = duration_mins
            break
            
    # Handle Recovery: If we improved below the highest threshold hit earlier
    # We allow clearing even if we were in a Hard Stop zone, provided current loss is lower than peak
    if highest_threshold_hit > 0 and app_t < highest_threshold_hit:
        if current_label != "recovery":
            log_with_callback(log_cb, f"📈 Recovery Detected: Net Loss {loss_amount:.0f} improved below {highest_threshold_hit}.")
            with open(BUY_DISABLED_FILE, 'w') as f:
                json.dump({
                    "disabled_until": 0, 
                    "last_trade_id": "recovery", 
                    "date": today_str, 
                    "highest_threshold": highest_threshold_hit
                }, f)
            log_with_callback(log_cb, "✅ Recovery: Buying re-enabled as losses are reducing.")
        return

    # Handle New/Higher Lockout
    # Trigger ONLY if current threshold is HIGHER than the peak hit (highest_threshold_hit)
    # This prevents re-triggering for same loss level after reset/refresh.
    if app_t > 0 and app_t > highest_threshold_hit:
        label = f"max_loss_{app_t}"
        disabled_until_ts = time.time() + (app_d * 60)
        with open(BUY_DISABLED_FILE, 'w') as f:
            json.dump({
                "disabled_until": disabled_until_ts, 
                "last_trade_id": label, 
                "date": today_str, 
                "highest_threshold": app_t
            }, f)
        dur_str = f"{app_d} mins" if app_d < 1440 else "tomorrow"
        log_with_callback(log_cb, f"⚠️ LOCKOUT: Increasing Loss hit {app_t}. (Prev Peak: {highest_threshold_hit}). Buy disabled for {dur_str}.")
        return

    if net_pnl >= BUY_DISABLE_MAX_PROFIT:
        if current_label != "max_profit":
             disabled_until_ts = time.time() + 86400 # 24 hours
             with open(BUY_DISABLED_FILE, 'w') as f:
                 json.dump({
                     "disabled_until": disabled_until_ts, 
                     "last_trade_id": "max_profit", 
                     "date": today_str,
                     "highest_threshold": highest_threshold_hit
                 }, f)
             log_with_callback(log_cb, f"SUCCESS: Net Profit {net_pnl} exceeds limit {BUY_DISABLE_MAX_PROFIT}. Buy disabled until tomorrow.")
        return

    # 2.5 Check for Total Daily Loss Count Hard Stop
    daily_losses = [t for t in completed if t.get("net_pnl", 0) < 0]
    if len(daily_losses) >= MAX_DAILY_LOSS_COUNT:
        if current_label != "max_loss_count":
            disabled_until_ts = time.time() + 86400 # 24 hours
            with open(BUY_DISABLED_FILE, 'w') as f:
                json.dump({
                    "disabled_until": disabled_until_ts, 
                    "last_trade_id": "max_loss_count", 
                    "date": today_str,
                    "highest_threshold": highest_threshold_hit
                }, f)
            log_with_callback(log_cb, f"⚠️ HARD STOP: Total daily loss count {len(daily_losses)} reached limit {MAX_DAILY_LOSS_COUNT}. Buy disabled until tomorrow.")
        return

    # 3. Check for NEW Consecutive Loss Lockout
    if len(completed) >= BUY_DISABLE_LOSS_COUNT:
        last_n = completed[-BUY_DISABLE_LOSS_COUNT:]
        # Use a unique ID for this set of losses to avoid re-triggering same lockout
        last_trade_time = last_n[-1].get("sell_time") or last_n[-1].get("buy_time")
        last_trade_ts = str(last_trade_time.timestamp()) if hasattr(last_trade_time, 'timestamp') else str(last_trade_time)
        
        if all(t.get("net_pnl", 0) < 0 for t in last_n) and last_trade_ts != current_label:
            disabled_until_ts = time.time() + BUY_DISABLE_DURATION
            with open(BUY_DISABLED_FILE, 'w') as f:
                json.dump({
                    "disabled_until": disabled_until_ts, 
                    "last_trade_id": last_trade_ts, 
                    "date": today_str,
                    "highest_threshold": highest_threshold_hit
                }, f)
            log_with_callback(log_cb, f"INFO: Buy disabled for {BUY_DISABLE_DURATION // 60} mins due to {BUY_DISABLE_LOSS_COUNT} losses.")
            return

    # 4. Check for Static Re-enable (Time passed or new day)
    if os.path.exists(BUY_DISABLED_FILE):
        try:
            # Check if lockout should be lifted based on time
            if time.time() >= disabled_until_ts:
                # Lockout time expired. We only clear it if we are also not currently in a threshold.
                if app_t == 0:
                    # UPDATE file instead of deleting, to keep the last_trade_id memory
                    with open(BUY_DISABLED_FILE, 'r') as f:
                        data = json.load(f)
                    data["disabled_until"] = 0 
                    with open(BUY_DISABLED_FILE, 'w') as f:
                        json.dump(data, f)
                    #log_with_callback(log_cb, f"✅ Lockout expired ({current_label}). Buy re-enabled.")
                return

            # Special recovery check for Max Profit
            if current_label == "max_profit" and net_pnl < BUY_DISABLE_MAX_PROFIT:
                os.remove(BUY_DISABLED_FILE)
                log_with_callback(log_cb, "✅ Profit Recovery: Buy re-enabled.")
                return

        except Exception as e:
            log_with_callback(log_cb, f"DEBUG: Re-enable check failed: {e}")

def save_trades_to_csv(trades_to_log):
    if not trades_to_log:
        return
    try:
        import csv
        os.makedirs("logs", exist_ok=True)
        filename = f"logs/trades_{datetime.now().strftime('%Y-%m-%d')}.csv"
        
        # Check existing Buy IDs in file to avoid logical duplicates
        existing_buy_ids = set()
        file_exists = os.path.exists(filename)
        if file_exists:
            try:
                with open(filename, "r") as r:
                    reader = csv.DictReader(r)
                    for row in reader:
                        bid = row.get("Buy ID")
                        if bid: existing_buy_ids.add(bid)
            except: pass

        with open(filename, "a", newline="") as f:
            headers = ["Symbol", "Trade Type", "Date", "Buy Time", "Sell Time", "Qty", "Buy Price", "Sell Price", "Gross PnL", "Charges", "Net PnL", 
                       "Buy ID", "Sell ID", "Order Source", "Exit Reason", "E_Sig", "E_EMA", "E_ROC", "E_BBW", "X_Sig", "X_EMA", "X_ROC", "X_BBW"]
            writer = csv.DictWriter(f, fieldnames=headers)
            if not file_exists:
                writer.writeheader()
            
            logged_count = 0
            for t in trades_to_log:
                # Skip if already in file
                bid = str(t.get("buy_order_id", ""))
                if bid in existing_buy_ids:
                    continue
                
                # Combine Buy and Sell sources for display
                b_src = t.get("order_source", "NA")
                s_src = t.get("sell_order_source", "NA")
                combined_src = f"B:{b_src} | S:{s_src}"
                if b_src == s_src: combined_src = b_src

                row = {
                    "Symbol": t["symbol"],
                    "Trade Type": t.get("trade_type", "Manual"),
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
                    "Order Source": combined_src,
                    "Exit Reason": t.get("exit_reason", "Manual/API"),
                    "E_Sig": t.get("entry_sig", "N/A"),
                    "E_EMA": t.get("entry_ema", "N/A"),
                    "E_ROC": t.get("entry_roc", "N/A"),
                    "E_BBW": t.get("entry_bbw", "N/A"),
                    "X_Sig": t.get("exit_sig", "N/A"),
                    "X_EMA": t.get("exit_ema", "N/A"),
                    "X_ROC": t.get("exit_roc", "N/A"),
                    "X_BBW": t.get("exit_bbw", "N/A")
                }
                writer.writerow(row)
                logged_count += 1
                
        if logged_count > 0:
            log_with_callback(log_cb, f"📝 {logged_count} new trades logged to {filename}")
    except Exception as e:
        log_with_callback(log_cb, f"CSV Error: {e}")

def render_monitor_display(lines, net_val, gross_val, day_net_val=0.0):
    global last_display_content
    current_content = "\n".join(lines)
    if current_content == last_display_content and last_display_content != "":
        return
    
    mon_text.config(state="normal")
    mon_text.delete("1.0", tk.END)
    
    for line in lines:
        if line.startswith("Recent:"):
            mon_text.insert(tk.END, "Recent: ")
            parts = line.split("Recent: ")[1].split(" | ")
            for i, p in enumerate(parts):
                if not p: continue
                try:
                    val = float(p)
                    tag = "green" if val > 0 else "red" if val < 0 else None
                except: tag = None
                mon_text.insert(tk.END, p, tag)
                if i < len(parts) - 1: mon_text.insert(tk.END, " | ")
            mon_text.insert(tk.END, "\n")
        else:
            tag = None
            if line.startswith("Net"): tag = "green" if net_val > 0 else "red"
            elif line.startswith("Gross"): tag = "green" if gross_val > 0 else "red"
            elif line.startswith("Day PnL"): tag = "green" if day_net_val > 0 else "red" if day_net_val < 0 else None
            elif "Allowed to Trade" in line: tag = "green"
            elif "Dont Trade" in line: tag = "red"
            mon_text.insert(tk.END, line + "\n", tag)
            
    mon_text.tag_config("green", foreground="#4ade80")
    mon_text.tag_config("red", foreground="#f87171")
    mon_text.config(state="disabled")
    last_display_content = current_content

# ---------------------------------------------------------
# AUTO STARTUP
# ---------------------------------------------------------
def startup_sequence():
    log_with_callback(log_cb, "🚀 Starting Auto-Login Sequence...")
    try:
        do_login(log_cb)
        load_scrip_master_csv(log_cb=log_cb)
        load_scrip_master_csv(log_cb=log_cb)
        log_with_callback(log_cb, "✅ Startup Complete")
        
        # Prepare Startup Message with Capital info
        startup_msg = "🚀 *NeoScalper Bot Initialized*\nWaiting for market data..."
        if os.path.exists(CAPITAL_HISTORY_FILE):
            try:
                df = pd.read_csv(CAPITAL_HISTORY_FILE)
                if not df.empty:
                    last_row = df.iloc[-1]
                    startup_msg += f"\n\n📊 *Last Session Summary ({last_row.get('Date', 'N/A')})*\n"
                    startup_msg += f"Initial: {float(last_row.get('Initial Capital', 0)):.2f}\n" 
                    startup_msg += f"Net PnL: {float(last_row.get('Net PnL', 0)):.2f}\n"
                    startup_msg += f"Closing: {float(last_row.get('Closing Capital', 0)):.2f}"
            except Exception as e:
                log_with_callback(log_cb, f"Error reading capital history for telegram: {e}")

        run_bg(send_telegram_msg, startup_msg)
        
        # Start Monitor First Run
        root.after(2000, lambda: run_bg(update_monitor_ui))
        
    except Exception as e:
        err_msg = str(e) if str(e) is not None else repr(e)
        log_with_callback(log_cb, f"❌ Startup Failed: {err_msg}")

root.after(500, lambda: run_bg(startup_sequence))
update_datetime_label()
update_status_label()
root.mainloop()
