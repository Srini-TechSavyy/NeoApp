from typing import Optional, Tuple

from web.shared.symbol_helpers import get_underlying_index


def fetch_index_ltp(client, symbol_hint: str) -> Tuple[float, Optional[str], Optional[str], Optional[str]]:
    """Return (ltp, instrument_token, exchange_segment, index_name) for symbol_hint."""
    idx_token, idx_exch, idx_name = get_underlying_index(symbol_hint)
    if not idx_token:
        return 0.0, None, None, None

    quote_resp = client.quotes(
        instrument_tokens=[{"instrument_token": idx_token, "exchange_segment": idx_exch}],
        quote_type="ltp",
    )
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


def fetch_index_ltp_for_base(base_symbol: str) -> Tuple[float, Optional[str]]:
    from common.orders import ensure_login

    base = str(base_symbol or "").strip().upper().replace("NIFTY_50", "NIFTY")
    if base not in ("NIFTY", "SENSEX"):
        raise ValueError("base_symbol must be NIFTY or SENSEX")

    client = ensure_login()
    ltp, _, _, idx_name = fetch_index_ltp(client, base)
    if ltp <= 0:
        raise ValueError(f"Could not fetch LTP for {base}")
    return ltp, idx_name
