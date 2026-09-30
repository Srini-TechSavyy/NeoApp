# scrip_master.py
import os
import re
import pandas as pd
from typing import Optional, Tuple
import time
from datetime import datetime
from .utils import log_with_callback
from .config import NSE_SCRIP_MASTER_PATH, BSE_SCRIP_MASTER_PATH
import requests

_scrip_master_df: Optional[pd.DataFrame] = None
_token_cache: dict = {}

_MONTH_LETTER_TO_NUM = {"O": 10, "N": 11, "D": 12}
_MONTH_NUM_TO_LETTER = {10: "O", 11: "N", 12: "D"}

def _scrip_master_files():
    """Resolve paths at call time so tests can patch the path constants."""
    return (
        ("nse_fo", NSE_SCRIP_MASTER_PATH, "NSE"),
        ("bse_fo", BSE_SCRIP_MASTER_PATH, "BSE"),
    )


def _scrip_master_is_current(path: str) -> bool:
    if not os.path.exists(path):
        return False
    mdate = datetime.fromtimestamp(os.path.getmtime(path)).date()
    return mdate == datetime.now().date()


def _refresh_stale_scrip_masters(log_cb=None) -> None:
    """Download any scrip master that is missing or not from today."""
    stale = [
        (segment, path, label)
        for segment, path, label in _scrip_master_files()
        if not _scrip_master_is_current(path)
    ]
    if not stale:
        return

    labels = ", ".join(label for _, _, label in stale)
    log_with_callback(log_cb, f"Scrip master outdated ({labels}). Downloading latest from Kotak...")
    try:
        # Prefer an existing authenticated session to avoid a second broker login.
        # Lazy import avoids circular import with common.orders.
        from .orders import ensure_login, get_client

        client = get_client()
        if client is None:
            client = ensure_login(log_cb=log_cb)
        headers = {"Authorization": f"Bearer {client.access_token}"} if hasattr(client, "access_token") else {}
        for segment, path, label in stale:
            url = client.scrip_master(exchange_segment=segment)
            if not isinstance(url, str) or not url.startswith("http"):
                log_with_callback(log_cb, f"Failed to resolve {label} scrip master URL: {url}")
                continue
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code == 200 and resp.content:
                with open(path, "wb") as f:
                    f.write(resp.content)
                log_with_callback(log_cb, f"Successfully downloaded new {label} scrip master!")
            else:
                log_with_callback(log_cb, f"Failed to download {label} scrip master. HTTP Details: {resp.status_code}")
    except Exception as dl_err:
        log_with_callback(log_cb, f"Download scrip master error: {dl_err}")


def load_scrip_master_csv(paths: Optional[list] = None, log_cb=None) -> None:
    """
    Load local scrip master CSVs and merge them.
    Normalizes column names to lowercase and strips semicolons.
    """
    global _scrip_master_df, _token_cache

    try:
        _refresh_stale_scrip_masters(log_cb=log_cb)
    except Exception as e:
        log_with_callback(log_cb, f"Scrip master update check failed: {e}")

    target_paths = paths or [NSE_SCRIP_MASTER_PATH, BSE_SCRIP_MASTER_PATH]
    
    all_dfs = []
    for path in target_paths:
        try:
            if not os.path.exists(path):
                log_with_callback(log_cb, f"Warning: Scrip master not found at: {path}")
                continue
            
            df = pd.read_csv(path, dtype=str)
            df.columns = [c.strip().lower().replace(";", "") for c in df.columns]
            all_dfs.append(df)
            log_with_callback(log_cb, f"Scrip master loaded from {path} ({len(df)} rows)")
        except Exception as e:
            log_with_callback(log_cb, f"ERROR loading scrip master from {path}: {e}")

    if all_dfs:
        _scrip_master_df = pd.concat(all_dfs, ignore_index=True)
        
        # Optimize: Pre-build token cache
        log_with_callback(log_cb, "Optimizing scrip master lookup index...")
        df = _scrip_master_df
        
        tr_col = next((c for c in df.columns if c in ("ptrdsymbol", "p_trd_symbol", "trading_symbol", "tradingsymbol")), None)
        if not tr_col:
            tr_col = next((c for c in df.columns if "trd" in c and "symbol" in c), None)
            
        token_col = next((c for c in df.columns if c in ("psymbol", "p_symbol", "token", "instrument_token")), None)
        if not token_col:
             token_col = next((c for c in df.columns if "psymbol" in c or (c.startswith("p") and "symbol" in c)), None)

        if tr_col and token_col:
            # Create mapping for fast lookup
            _token_cache = dict(zip(
                df[tr_col].fillna("").astype(str).str.strip().str.upper(),
                df[token_col].fillna("").astype(str).str.strip()
            ))
            
        log_with_callback(log_cb, f"Combined scrip master ready ({len(_scrip_master_df)} rows, {len(_token_cache)} tokens cached)")
        # Sample check
        sample_key = list(_token_cache.keys())[0] if _token_cache else "NONE"
        log_with_callback(log_cb, f"DEBUG: Cache sample: {sample_key} -> {_token_cache.get(sample_key)}")
    else:
        log_with_callback(log_cb, "ERROR: No scrip master files loaded.")
        raise FileNotFoundError("No valid scrip master files found.")

def get_lot_size_from_scrip_master(trading_symbol: str, default=1) -> int:
    if _scrip_master_df is None:
        return default
    
    ts = trading_symbol.strip().upper()
    # Prefer resolved broker symbol so lot size hits after expiry-format drift.
    resolved = resolve_trading_symbol(ts)
    if resolved:
        ts = resolved
    df = _scrip_master_df

    tr_col = next((c for c in df.columns if "trd" in c and "symbol" in c), None)
    if not tr_col: return default

    lot_col = next((c for c in df.columns if "lot" in c and "size" in c), None)
    if not lot_col: return default

    row = df[df[tr_col].astype(str).str.upper() == ts]
    if row.empty: return default

    try:
        return int(float(row.iloc[0][lot_col]))
    except:
        return default


def _parse_option_symbol(symbol: str) -> Optional[Tuple[str, str, int, str]]:
    """Parse option trading symbol into (base, expiry, strike, opt_type)."""
    s = str(symbol).strip().upper()

    m = re.search(r'^([A-Z]+?)([0-9]{6})(\d+)(CE|PE)$', s)
    if m:
        base, expiry, strike, opt_type = m.groups()
        try:
            mm = int(expiry[2:4])
            dd = int(expiry[4:6])
            if 1 <= mm <= 12 and 1 <= dd <= 31:
                return base, expiry, int(strike), opt_type
        except Exception:
            pass

    m = re.search(r'^([A-Z]+?)([0-9]{2}[A-Z]{3})(\d+)(CE|PE)$', s)
    if m:
        base, expiry, strike, opt_type = m.groups()
        return base, expiry, int(strike), opt_type

    m = re.search(r'^([A-Z]+?)([0-9]{2}[1-9OND][0-9]{2})(\d+)(CE|PE)$', s)
    if m:
        base, expiry, strike, opt_type = m.groups()
        return base, expiry, int(strike), opt_type

    m = re.search(r'^([A-Z]+?)([0-9]{5})(\d+)(CE|PE)$', s)
    if m:
        base, expiry, strike, opt_type = m.groups()
        return base, expiry, int(strike), opt_type

    return None


def _normalize_weekly_expiry(expiry: str) -> str:
    """Map legacy YYMMDD (e.g. 261001) to Kotak weekly code (26O01)."""
    e = str(expiry).strip().upper()
    if len(e) == 6 and e.isdigit():
        yy, mm, dd = e[:2], int(e[2:4]), e[4:6]
        if mm in _MONTH_NUM_TO_LETTER:
            return f"{yy}{_MONTH_NUM_TO_LETTER[mm]}{dd}"
    return e


def _expiry_sort_key(expiry: str) -> tuple:
    """Rough chronological key for weekly/monthly expiry codes."""
    e = _normalize_weekly_expiry(expiry)
    try:
        if len(e) == 5 and e[2] in "123456789OND":
            yy = 2000 + int(e[:2])
            month_ch = e[2]
            mm = _MONTH_LETTER_TO_NUM.get(month_ch, int(month_ch))
            dd = int(e[3:5])
            return (yy, mm, dd)
        if len(e) == 5 and e[2:].isalpha():
            # Monthly YYMMM — use mid-month as approximate
            yy = 2000 + int(e[:2])
            mm = datetime.strptime(e[2:], "%b").month
            return (yy, mm, 28)
    except Exception:
        pass
    return (9999, 12, 31)


def _token_cache_keys():
    if _token_cache:
        return _token_cache.keys()
    if _scrip_master_df is None:
        return []
    df = _scrip_master_df
    tr_col = next((c for c in df.columns if "trd" in c and "symbol" in c), None)
    if not tr_col:
        return []
    return df[tr_col].fillna("").astype(str).str.strip().str.upper().tolist()


def _lookup_token_exact(ts: str) -> Optional[str]:
    if _token_cache:
        return _token_cache.get(ts)
    if _scrip_master_df is None:
        return None
    df = _scrip_master_df
    tr_col = next((c for c in df.columns if "trd" in c and "symbol" in c), None)
    token_col = next((c for c in df.columns if "token" in c or "psymbol" in c), None)
    if not tr_col or not token_col:
        return None
    matches = df[df[tr_col].astype(str).str.upper() == ts]
    if matches.empty:
        return None
    return str(matches.iloc[0][token_col]).strip()


def resolve_trading_symbol(trading_symbol: str, log_cb=None) -> Optional[str]:
    """
    Return the broker trading symbol from scrip master.
    Exact match first; on miss, match by base + strike + CE/PE and prefer
    the intended (or normalized) expiry code from today's scrip master.
    """
    ts = str(trading_symbol).strip().upper()
    if not ts:
        return None

    if _lookup_token_exact(ts) is not None:
        return ts

    parts = _parse_option_symbol(ts)
    if not parts:
        return None

    base, expiry, strike, opt_type = parts
    suffix = f"{strike}{opt_type}"
    intended = _normalize_weekly_expiry(expiry)

    candidates = []
    for key in _token_cache_keys():
        if not key.startswith(base) or not key.endswith(suffix):
            continue
        cand_parts = _parse_option_symbol(key)
        if not cand_parts:
            continue
        c_base, c_expiry, c_strike, c_opt = cand_parts
        if c_base != base or c_strike != strike or c_opt != opt_type:
            continue
        candidates.append((key, c_expiry))

    if not candidates:
        return None

    for key, c_expiry in candidates:
        if c_expiry == expiry or _normalize_weekly_expiry(c_expiry) == intended:
            if key != ts:
                log_with_callback(log_cb, f"Resolved trading symbol {ts} -> {key}")
            return key

    # Prefer nearest future expiry; fall back to lexicographically first.
    today_key = (datetime.now().year, datetime.now().month, datetime.now().day)

    def rank(item):
        key, c_expiry = item
        ek = _expiry_sort_key(c_expiry)
        future_penalty = 0 if ek >= today_key else 1
        return (future_penalty, ek, key)

    best = sorted(candidates, key=rank)[0][0]
    log_with_callback(log_cb, f"Resolved trading symbol {ts} -> {best} (nearest expiry match)")
    return best


def find_token_for_trading_symbol(trading_symbol: str, log_cb=None) -> Optional[str]:
    """
    Finds server-side token for the trading symbol using optimized cache.
    Falls back to scrip-master resolution when the constructed symbol drifts
    from the broker's expiry encoding (e.g. 261001 vs 26O01).
    """
    resolved = resolve_trading_symbol(trading_symbol, log_cb=log_cb)
    if not resolved:
        return None
    return _lookup_token_exact(resolved)
