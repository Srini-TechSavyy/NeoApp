from collections import defaultdict
import logging
import threading
import time
from typing import Dict, List, Optional, Tuple

from common.orders import detect_exchange_segment, ensure_login, place_market_order
from common.scrip_master import (
    find_token_for_trading_symbol,
    get_lot_size_from_scrip_master,
    load_scrip_master_csv,
    resolve_trading_symbol,
    validate_trading_symbol,
)
from web.shared.order_status import lookup_order_status
from web.shared.trade_latency import TradeLatencyRecorder

logger = logging.getLogger("neoapp.web.trading_actions")

_SYMBOLS_LOADED = False
_SYMBOLS_LOCK = threading.Lock()


def _ensure_symbols_loaded(latency: Optional[TradeLatencyRecorder] = None) -> None:
    """Load scrip master once; concurrent callers share a single load."""
    global _SYMBOLS_LOADED
    if _SYMBOLS_LOADED:
        return
    with _SYMBOLS_LOCK:
        if _SYMBOLS_LOADED:
            return
        load_scrip_master_csv()
        _SYMBOLS_LOADED = True
        if latency:
            latency.mark("scrip_master_load")


def preload_scrip_master() -> bool:
    """
    Eagerly load the scrip master (startup path).
    Returns True on success. Failures are logged and do not raise so callers
    can keep serving while lazy-load remains available.
    """
    logger.info("scrip_master_preload_start")
    t0 = time.perf_counter()
    try:
        _ensure_symbols_loaded()
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        logger.info(
            "scrip_master_preload_success scrip_master_preload_duration_ms=%.1f",
            duration_ms,
        )
        return True
    except Exception:
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        logger.exception(
            "scrip_master_preload_failed scrip_master_preload_duration_ms=%.1f",
            duration_ms,
        )
        return False


def is_scrip_master_loaded() -> bool:
    return _SYMBOLS_LOADED


def _normalize_list_payload(payload) -> List[dict]:
    if isinstance(payload, dict):
        data = payload.get("data", [])
        return data if isinstance(data, list) else []
    if isinstance(payload, list):
        return payload
    return []


def _get_open_positions(client) -> List[dict]:
    return _normalize_list_payload(client.positions())


def _has_open_derivative_position(positions: List[dict]) -> Tuple[bool, Optional[str]]:
    net_by_sym: Dict[str, int] = defaultdict(int)
    for p in positions:
        sym = str(p.get("trdSym", "")).strip().upper()
        exch = str(p.get("exch", "")).lower()
        if not sym or "-EQ" in sym:
            continue
        if "fo" in exch or any(x in sym for x in ["NIFTY", "SENSEX", "BANKNIFTY", "FINNIFTY", "BANKEX"]):
            fl_buy = float(p.get("flBuyQty", 0) or 0)
            fl_sell = float(p.get("flSellQty", 0) or 0)
            cf_buy = float(p.get("cfBuyQty", 0) or 0)
            cf_sell = float(p.get("cfSellQty", 0) or 0)
            raw_net = p.get("netQty")
            if raw_net is not None and str(raw_net).strip() != "":
                row_qty = int(float(str(raw_net)))
            else:
                row_qty = int(float((cf_buy + fl_buy) - (cf_sell + fl_sell)))
            net_by_sym[sym] += row_qty

    for sym, qty in net_by_sym.items():
        if qty != 0:
            return True, sym
    return False, None


def _is_order_completed(
    client,
    order_id: str,
    latency: Optional[TradeLatencyRecorder] = None,
) -> Tuple[bool, str, Optional[float]]:
    status = "unknown"
    exec_price = None
    poll_details: List[dict] = []

    for attempt in range(6):
        sleep_ms = 500
        time.sleep(0.5)
        t_poll = time.perf_counter()
        history = _normalize_list_payload(client.order_history(order_id=order_id))
        api_ms = round((time.perf_counter() - t_poll) * 1000, 1)
        poll_details.append({"attempt": attempt + 1, "sleep_ms": sleep_ms, "order_history_ms": api_ms, "rows": len(history)})
        if not history:
            continue
        latest = history[-1]
        status = str(latest.get("ordSt", "")).lower()
        if status == "complete":
            norm, exec_price, _ = lookup_order_status(client, order_id)
            status = norm if norm != "unknown" else status
            if latency:
                latency.add_meta(order_polls=poll_details, order_poll_attempts=attempt + 1, order_final_status=status)
            return True, status, exec_price
        if status == "rejected":
            if latency:
                latency.add_meta(order_polls=poll_details, order_poll_attempts=attempt + 1, order_final_status=status)
            return False, status, None

    t_report = time.perf_counter()
    rep_data = _normalize_list_payload(client.order_report())
    report_ms = round((time.perf_counter() - t_report) * 1000, 1)
    poll_details.append({"fallback": "order_report", "order_report_ms": report_ms, "rows": len(rep_data)})
    for o in rep_data:
        if str(o.get("nOrdNo", "")) == str(order_id):
            status = str(o.get("ordSt", "")).lower()
            if status == "complete":
                avg_p = o.get("avgPrc") or o.get("buyAvgPrc") or o.get("sellAvgPrc") or o.get("price")
                exec_price = float(avg_p) if avg_p else None
                if latency:
                    latency.add_meta(order_polls=poll_details, order_poll_attempts=6, order_final_status=status)
                return True, status, exec_price
            if latency:
                latency.add_meta(order_polls=poll_details, order_poll_attempts=6, order_final_status=status)
            return False, status, None

    if latency:
        latency.add_meta(order_polls=poll_details, order_poll_attempts=6, order_final_status=status)
    return False, status, exec_price


def _convert_quantity_to_lots(trading_symbol: str, quantity: int) -> int:
    lot_size = max(int(get_lot_size_from_scrip_master(trading_symbol, default=1)), 1)
    lots = round(abs(quantity) / lot_size)
    return max(1, int(lots))


def execute_market_action(
    trading_symbol: str,
    lots: int,
    action: str,
    enforce_single_position: bool = True,
    latency: Optional[TradeLatencyRecorder] = None,
    wait_for_completion: bool = True,
) -> Dict:
    meta = latency.meta if latency is not None else None

    _ensure_symbols_loaded(latency)
    if latency:
        latency.mark("after_scrip_master")

    client = ensure_login(latency_meta=meta)
    if latency:
        latency.mark("after_neo_login")

    symbol = trading_symbol.strip().upper()
    side = "BUY" if action.upper() == "BUY" else "SELL"

    positions = _get_open_positions(client)
    if latency:
        latency.mark("after_positions_api")
    has_pos, pos_sym = _has_open_derivative_position(positions)

    selected_symbol = symbol
    selected_lots = int(lots)
    selected_product = "MIS"
    selected_qty = None
    token = None

    if side == "BUY":
        validation = validate_trading_symbol(symbol)
        if not validation.get("ok"):
            raise ValueError(validation.get("reason") or "invalid option contract for BUY")
        selected_symbol = str(validation["trading_symbol"]).upper()
        token = str(validation["instrument_token"])
        logger.info(
            "order_contract_validated symbol=%s expiry=%s strike=%s opt_type=%s instrument_token=%s",
            selected_symbol,
            validation.get("expiry_token"),
            validation.get("strike"),
            validation.get("opt_type"),
            token,
        )
        if enforce_single_position and has_pos:
            raise ValueError(f"Buy blocked: existing open position detected ({pos_sym})")
    else:
        # SELL / EXIT logic: identify target open position FIRST
        target_pos = None
        cleaned_sym = symbol.replace(" ", "")
        for p in positions:
            p_sym = str(p.get("trdSym", "")).strip().upper().replace(" ", "")
            if p_sym == cleaned_sym:
                target_pos = p
                break
        if not target_pos and has_pos:
            for p in positions:
                p_sym = str(p.get("trdSym", "")).strip().upper()
                if not p_sym or "-EQ" in p_sym:
                    continue
                exch_p = str(p.get("exch", "")).lower()
                if "fo" in exch_p or any(x in p_sym for x in ["NIFTY", "SENSEX", "BANKNIFTY", "FINNIFTY", "BANKEX"]):
                    fl_buy = float(p.get("flBuyQty", 0) or 0)
                    fl_sell = float(p.get("flSellQty", 0) or 0)
                    cf_buy = float(p.get("cfBuyQty", 0) or 0)
                    cf_sell = float(p.get("cfSellQty", 0) or 0)
                    raw_net = p.get("netQty")
                    if raw_net is not None and str(raw_net).strip() != "":
                        row_net = int(float(str(raw_net)))
                    else:
                        row_net = int(float((cf_buy + fl_buy) - (cf_sell + fl_sell)))
                    if row_net != 0:
                        target_pos = p
                        break

        if target_pos:
            selected_symbol = str(target_pos.get("trdSym", symbol)).strip().upper()
            selected_product = str(target_pos.get("prod") or target_pos.get("prd") or "MIS").strip().upper()
            fl_buy = float(target_pos.get("flBuyQty", 0) or 0)
            fl_sell = float(target_pos.get("flSellQty", 0) or 0)
            cf_buy = float(target_pos.get("cfBuyQty", 0) or 0)
            cf_sell = float(target_pos.get("cfSellQty", 0) or 0)
            raw_net = target_pos.get("netQty")
            if raw_net is not None and str(raw_net).strip() != "":
                net_qty = int(float(str(raw_net)))
            else:
                net_qty = int(float((cf_buy + fl_buy) - (cf_sell + fl_sell)))

            selected_qty = abs(net_qty) if net_qty != 0 else None
            selected_lots = _convert_quantity_to_lots(selected_symbol, net_qty) if net_qty != 0 else int(lots)
            token = find_token_for_trading_symbol(selected_symbol)
            if not token:
                load_scrip_master_csv()
                resolved = resolve_trading_symbol(selected_symbol) or selected_symbol
                selected_symbol = resolved
                token = find_token_for_trading_symbol(selected_symbol)
        elif enforce_single_position:
            raise ValueError("Exit blocked: no open derivative position found")
        else:
            resolved = resolve_trading_symbol(symbol) or symbol
            selected_symbol = resolved
            token = find_token_for_trading_symbol(selected_symbol)
            if not token:
                load_scrip_master_csv()
                resolved = resolve_trading_symbol(symbol) or symbol
                selected_symbol = resolved
                token = find_token_for_trading_symbol(selected_symbol)

    if latency:
        latency.mark("after_symbol_lookup")

    if not token:
        raise ValueError(f"Token not found for symbol: {selected_symbol}")

    if latency:
        latency.mark("after_pre_order_logic")

    resp = place_market_order(
        token=token,
        lots=selected_lots,
        side=side,
        trading_symbol=selected_symbol,
        latency_meta=meta,
        product=selected_product,
        quantity=selected_qty,
    )
    if latency:
        latency.mark("after_place_order_api")

    if not (isinstance(resp, dict) and resp.get("nOrdNo")):
        err = resp.get("errMsg") if isinstance(resp, dict) else "Invalid order response"
        raise ValueError(f"Order failed: {err}")

    order_id = str(resp.get("nOrdNo"))
    if latency:
        latency.add_meta(broker_order_id=order_id, place_order_ack=True)
        latency.mark("after_order_placement_ack")

    if not wait_for_completion:
        if latency:
            latency.mark("immediate_response_ready")
            latency.add_meta(async_status_confirmation=True, order_completed=False, broker_status="submitted")
        return {
            "symbol": selected_symbol,
            "lots": selected_lots,
            "side": side,
            "order_id": order_id,
            "nOrdNo": order_id,
            "submitted": True,
            "completed": False,
            "status": "submitted",
            "message": "Order submitted; awaiting fill confirmation",
            "execution_price": None,
            "raw": resp,
            "exchange_segment": detect_exchange_segment(selected_symbol),
        }

    completed, status, exec_price = _is_order_completed(client, order_id, latency=latency)
    if latency:
        latency.mark("after_order_completion_poll")
        latency.add_meta(order_completed=completed, broker_status=status)

    return {
        "symbol": selected_symbol,
        "lots": selected_lots,
        "side": side,
        "order_id": order_id,
        "nOrdNo": order_id,
        "submitted": True,
        "completed": completed,
        "status": status,
        "message": "Order filled" if completed and status == "complete" else status,
        "execution_price": exec_price,
        "raw": resp,
        "exchange_segment": detect_exchange_segment(selected_symbol),
    }
