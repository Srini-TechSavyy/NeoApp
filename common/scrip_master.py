# scrip_master.py
import os
import pandas as pd
from typing import Optional
import time
from datetime import datetime
from .utils import log_with_callback
from .config import NSE_SCRIP_MASTER_PATH, BSE_SCRIP_MASTER_PATH
import requests

_scrip_master_df: Optional[pd.DataFrame] = None
_token_cache: dict = {}

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

def find_token_for_trading_symbol(trading_symbol: str, log_cb=None) -> Optional[str]:
    """
    Finds server-side token for the exact trading symbol using optimized cache.
    """
    if not _token_cache:
        # Fallback to slow search if cache not populated
        if _scrip_master_df is None: return None
        
        ts = str(trading_symbol).strip().upper()
        df = _scrip_master_df
        tr_col = next((c for c in df.columns if "trd" in c and "symbol" in c), None)
        token_col = next((c for c in df.columns if "token" in c or "psymbol" in c), None)
        if tr_col and token_col:
            matches = df[df[tr_col].astype(str).str.upper() == ts]
            if not matches.empty:
                return str(matches.iloc[0][token_col]).strip()
        return None
    
    ts = str(trading_symbol).strip().upper()
    return _token_cache.get(ts)
