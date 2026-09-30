import re
from typing import Optional, Tuple

from common.config import get_next_expiry
from common.orders import detect_strike_step


def get_underlying_index(symbol: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    s = symbol.upper()
    if "NIFTY" in s:
        if "BANK" in s:
            return "Nifty Bank", "nse_cm", "BANKNIFTY"
        if "FIN" in s:
            return "Nifty Fin Services", "nse_cm", "FINNIFTY"
        return "Nifty 50", "nse_cm", "NIFTY"
    if "SENSEX" in s or "BSX" in s:
        return "SENSEX", "bse_cm", "SENSEX"
    if "BANKEX" in s:
        return "BANKEX", "bse_cm", "BANKEX"
    return None, None, None


def parse_symbol_parts(symbol: str):
    s = symbol.strip().upper()

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

    # Monthly: YYMMM (e.g. 26SEP)
    m = re.search(r'^([A-Z]+?)([0-9]{2}[A-Z]{3})(\d+)(CE|PE)$', s)
    if m:
        base, expiry, strike, opt_type = m.groups()
        return base, expiry, int(strike), opt_type

    # Weekly: YY + month code [1-9OND] + DD (e.g. 26917, 26O01)
    m = re.search(r'^([A-Z]+?)([0-9]{2}[1-9OND][0-9]{2})(\d+)(CE|PE)$', s)
    if m:
        base, expiry, strike, opt_type = m.groups()
        return base, expiry, int(strike), opt_type

    m = re.search(r'^([A-Z]+?)([0-9]{5})(\d+)(CE|PE)$', s)
    if m:
        base, expiry, strike, opt_type = m.groups()
        return base, expiry, int(strike), opt_type

    return None


def suggest_option_symbol(base_symbol: str, index_ltp: float, option_type: str, offset: int = 0) -> str:
    base = base_symbol.strip().upper().replace("NIFTY_50", "NIFTY")
    opt_type = option_type.strip().upper()
    if opt_type not in ("CE", "PE"):
        raise ValueError("option_type must be CE or PE")

    strike_step = int(detect_strike_step(base))
    atm = round(float(index_ltp) / strike_step) * strike_step

    if opt_type == "CE":
        strike = int(atm + (offset * strike_step))
    else:
        strike = int(atm - (offset * strike_step))

    expiry = get_next_expiry(base)
    return f"{base}{expiry}{strike}{opt_type}"
