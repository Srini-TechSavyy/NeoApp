"""Single-shot broker order status lookup (no polling sleeps)."""

from typing import List, Optional, Tuple


def _normalize_list_payload(payload) -> List[dict]:
    if isinstance(payload, dict):
        data = payload.get("data", [])
        return data if isinstance(data, list) else []
    if isinstance(payload, list):
        return payload
    return []


def _execution_price_from_row(row: dict) -> Optional[float]:
    avg_p = row.get("avgPrc") or row.get("buyAvgPrc") or row.get("sellAvgPrc") or row.get("price")
    if avg_p and float(avg_p) > 0:
        return float(avg_p)
    return None


def normalize_broker_status(status: str) -> str:
    s = str(status or "").lower().strip()
    if s in ("complete", "completed", "filled"):
        return "complete"
    if s in ("rejected", "reject"):
        return "rejected"
    if s in ("cancelled", "canceled", "cancel"):
        return "cancelled"
    if s in ("open", "trigger pending", "pending"):
        return "pending"
    if s in ("submitted", "validation pending", "put order req received"):
        return "submitted"
    if not s:
        return "unknown"
    return s


def is_terminal_status(status: str) -> bool:
    norm = normalize_broker_status(status)
    return norm in ("complete", "rejected", "cancelled")


def lookup_order_status(client, order_id: str) -> Tuple[str, Optional[float], bool]:
    """
    Query broker once for order_id.

    Returns (normalized_status, execution_price_if_complete, found_on_broker).
    """
    order_id = str(order_id)
    history = _normalize_list_payload(client.order_history(order_id=order_id))
    if history:
        latest = history[-1]
        raw_status = str(latest.get("ordSt", ""))
        norm = normalize_broker_status(raw_status)
        exec_price = None
        if norm == "complete":
            for state in reversed(history):
                exec_price = _execution_price_from_row(state)
                if exec_price:
                    break
        return norm, exec_price, True

    rep_data = _normalize_list_payload(client.order_report())
    for o in rep_data:
        if str(o.get("nOrdNo", "")) == order_id:
            raw_status = str(o.get("ordSt", ""))
            norm = normalize_broker_status(raw_status)
            exec_price = _execution_price_from_row(o) if norm == "complete" else None
            return norm, exec_price, True

    return "unknown", None, False
