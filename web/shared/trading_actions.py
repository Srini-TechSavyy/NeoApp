import time
from typing import Dict, List, Optional, Tuple

from common.orders import detect_exchange_segment, ensure_login, place_market_order
from common.scrip_master import find_token_for_trading_symbol, get_lot_size_from_scrip_master, load_scrip_master_csv
from web.shared.order_status import lookup_order_status
from web.shared.trade_latency import TradeLatencyRecorder

_SYMBOLS_LOADED = False


def _ensure_symbols_loaded(latency: Optional[TradeLatencyRecorder] = None) -> None:
    global _SYMBOLS_LOADED
    if not _SYMBOLS_LOADED:
        load_scrip_master_csv()
        _SYMBOLS_LOADED = True
        if latency:
            latency.mark("scrip_master_load")


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
    for p in positions:
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
    token = find_token_for_trading_symbol(symbol)
    if not token:
        load_scrip_master_csv()
        token = find_token_for_trading_symbol(symbol)
    if latency:
        latency.mark("after_symbol_lookup")

    if not token:
        raise ValueError(f"Token not found for symbol: {symbol}")

    side = "BUY" if action.upper() == "BUY" else "SELL"

    positions = _get_open_positions(client)
    if latency:
        latency.mark("after_positions_api")
    has_pos, pos_sym = _has_open_derivative_position(positions)

    if side == "BUY" and enforce_single_position and has_pos:
        raise ValueError(f"Buy blocked: existing open position detected ({pos_sym})")

    selected_symbol = symbol
    selected_lots = int(lots)
    if side == "SELL":
        target_pos = None
        for p in positions:
            sym = str(p.get("trdSym", "")).upper()
            if sym == symbol:
                target_pos = p
                break
        if not target_pos and has_pos:
            for p in positions:
                sym = str(p.get("trdSym", "")).upper()
                fl_buy = float(p.get("flBuyQty", 0) or 0)
                fl_sell = float(p.get("flSellQty", 0) or 0)
                qty = int(float(p.get("netQty", fl_buy - fl_sell) or 0))
                if qty != 0 and "-EQ" not in sym:
                    target_pos = p
                    break

        if target_pos:
            selected_symbol = str(target_pos.get("trdSym", symbol)).upper()
            fl_buy = float(target_pos.get("flBuyQty", 0) or 0)
            fl_sell = float(target_pos.get("flSellQty", 0) or 0)
            net_qty = int(float(target_pos.get("netQty", fl_buy - fl_sell) or 0))
            selected_lots = _convert_quantity_to_lots(selected_symbol, net_qty)
            token = find_token_for_trading_symbol(selected_symbol) or token
        elif enforce_single_position:
            raise ValueError("Exit blocked: no open derivative position found")

    if latency:
        latency.mark("after_pre_order_logic")

    resp = place_market_order(
        token=token,
        lots=selected_lots,
        side=side,
        trading_symbol=selected_symbol,
        latency_meta=meta,
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
