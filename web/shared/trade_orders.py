"""Persist pending web trades and idempotent trade responses."""

import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from web.shared.order_status import is_terminal_status, lookup_order_status

try:
    import fcntl
except Exception:  # pragma: no cover
    fcntl = None

logger = logging.getLogger("neoapp.trade.orders")

def _pending_orders_file() -> str:
    return os.getenv("WEB_PENDING_ORDERS_FILE", "logs/web_pending_orders.json")


def _idempotency_file() -> str:
    return os.getenv("WEB_TRADE_IDEMPOTENCY_FILE", "logs/web_trade_idempotency.json")


def _idempotency_ttl_seconds() -> int:
    return int(os.getenv("WEB_TRADE_IDEMPOTENCY_TTL_SECONDS", str(24 * 3600)))


def _max_pending_orders() -> int:
    return int(os.getenv("WEB_MAX_PENDING_ORDERS", "50"))


def _abs_path(path: str) -> str:
    if os.path.isabs(path):
        return path
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, path)


@contextmanager
def _file_lock(path: str, shared: bool):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a+", encoding="utf-8") as lock_f:
        if fcntl:
            mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            fcntl.flock(lock_f.fileno(), mode)
        try:
            yield
        finally:
            if fcntl:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)


def _read_json(path: str, default):
    abs_path = _abs_path(path)
    if not os.path.exists(abs_path):
        return default
    lock_path = f"{abs_path}.lock"
    with _file_lock(lock_path, shared=True):
        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default


def _write_json(path: str, data) -> None:
    abs_path = _abs_path(path)
    os.makedirs(os.path.dirname(abs_path) or ".", exist_ok=True)
    lock_path = f"{abs_path}.lock"
    with _file_lock(lock_path, shared=False):
        fd, temp_path = tempfile.mkstemp(prefix="trade_state_", suffix=".json", dir=os.path.dirname(abs_path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, abs_path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _prune_idempotency(records: Dict[str, Any]) -> Dict[str, Any]:
    cutoff = time.time() - _idempotency_ttl_seconds()
    out = {}
    for key, row in records.items():
        if not isinstance(row, dict):
            continue
        ts = float(row.get("saved_at", 0) or 0)
        if ts >= cutoff:
            out[key] = row
    return out


def find_pending_by_client_request_id(client_request_id: str) -> Optional[Dict[str, Any]]:
    key = str(client_request_id or "").strip()
    if not key:
        return None
    for row in list_pending_orders():
        if str(row.get("client_request_id", "")) == key:
            return row
    return None


def pending_order_to_api_response(row: Dict[str, Any]) -> Dict[str, Any]:
    action = str(row.get("action") or ("EXIT" if row.get("side") == "SELL" else "BUY"))
    symbol = str(row.get("symbol") or "")
    lots = int(row.get("lots") or 1)
    order_id = str(row.get("order_id") or "")
    status = str(row.get("status") or "submitted")
    completed = bool(row.get("completed"))
    return {
        "ok": True,
        "action": action,
        "trading_symbol": symbol,
        "lots": lots,
        "request_id": row.get("request_id"),
        "broker_response": {
            "symbol": symbol,
            "lots": lots,
            "side": row.get("side"),
            "order_id": order_id,
            "nOrdNo": order_id,
            "submitted": True,
            "completed": completed,
            "status": status,
            "message": row.get("message"),
            "execution_price": row.get("execution_price"),
        },
    }


def get_idempotent_trade_response(client_request_id: str) -> Optional[Dict[str, Any]]:
    key = str(client_request_id or "").strip()
    if not key:
        return None
    store = _read_json(_idempotency_file(), {"records": {}})
    records = _prune_idempotency(store.get("records", {}))
    row = records.get(key)
    if not isinstance(row, dict):
        return None
    if row.get("state") == "in_flight":
        return {"in_flight": True}
    if row.get("state") == "done" and isinstance(row.get("api_response"), dict):
        return row["api_response"]
    return None


def begin_idempotent_trade(client_request_id: str) -> Optional[Dict[str, Any]]:
    """Return cached API response, or None if this request should proceed. Raises ValueError if in flight."""
    key = str(client_request_id or "").strip()
    if not key:
        return None
    store = _read_json(_idempotency_file(), {"records": {}})
    records = _prune_idempotency(store.get("records", {}))
    row = records.get(key)
    if isinstance(row, dict) and row.get("state") == "done" and isinstance(row.get("api_response"), dict):
        return row["api_response"]
    if isinstance(row, dict) and row.get("state") == "in_flight":
        pending = find_pending_by_client_request_id(key)
        if pending:
            api_response = pending_order_to_api_response(pending)
            complete_idempotent_trade(key, api_response)
            return api_response
        raise ValueError("Duplicate trade request already in progress for this client_request_id")
    records[key] = {"state": "in_flight", "saved_at": time.time(), "started_at": _utc_now()}
    store["records"] = records
    _write_json(_idempotency_file(), store)
    return None


def complete_idempotent_trade(client_request_id: str, api_response: Dict[str, Any]) -> None:
    key = str(client_request_id or "").strip()
    if not key:
        return
    store = _read_json(_idempotency_file(), {"records": {}})
    records = _prune_idempotency(store.get("records", {}))
    records[key] = {
        "state": "done",
        "saved_at": time.time(),
        "completed_at": _utc_now(),
        "api_response": api_response,
    }
    store["records"] = records
    _write_json(_idempotency_file(), store)


def abort_idempotent_trade(client_request_id: str) -> None:
    key = str(client_request_id or "").strip()
    if not key:
        return
    store = _read_json(_idempotency_file(), {"records": {}})
    records = _prune_idempotency(store.get("records", {}))
    if records.get(key, {}).get("state") == "in_flight":
        records.pop(key, None)
    store["records"] = records
    _write_json(_idempotency_file(), store)


def register_pending_order(
    *,
    request_id: str,
    order_id: str,
    symbol: str,
    side: str,
    lots: int,
    client_request_id: Optional[str] = None,
    action: str = "",
) -> Dict[str, Any]:
    record = {
        "request_id": request_id,
        "client_request_id": client_request_id or "",
        "order_id": str(order_id),
        "symbol": symbol,
        "side": side,
        "action": action or ("EXIT" if side == "SELL" else "BUY"),
        "lots": int(lots),
        "submitted": True,
        "completed": False,
        "status": "submitted",
        "execution_price": None,
        "message": "Order submitted; awaiting fill confirmation",
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "confirmed_at": None,
    }
    store = _read_json(_pending_orders_file(), {"orders": []})
    orders: List[dict] = store.get("orders", []) if isinstance(store.get("orders"), list) else []
    orders = [o for o in orders if str(o.get("order_id")) != str(order_id)]
    orders.append(record)
    orders.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    store["orders"] = orders[:_max_pending_orders()]
    _write_json(_pending_orders_file(), store)
    return record


def list_pending_orders() -> List[Dict[str, Any]]:
    store = _read_json(_pending_orders_file(), {"orders": []})
    orders = store.get("orders", []) if isinstance(store.get("orders"), list) else []
    return list(orders)


def refresh_pending_orders(client, latency_meta: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Poll broker once per pending order; returns updated order list."""
    store = _read_json(_pending_orders_file(), {"orders": []})
    orders: List[dict] = store.get("orders", []) if isinstance(store.get("orders"), list) else []
    if not orders:
        return []

    t0 = time.perf_counter()
    updates = 0
    refreshed: List[dict] = []

    for row in orders:
        if not isinstance(row, dict):
            continue
        order_id = str(row.get("order_id", "")).strip()
        if not order_id:
            continue
        if row.get("completed") and is_terminal_status(str(row.get("status", ""))):
            refreshed.append(row)
            continue

        t_one = time.perf_counter()
        status, exec_price, found = lookup_order_status(client, order_id)
        one_ms = round((time.perf_counter() - t_one) * 1000, 1)
        prev_status = str(row.get("status", ""))
        row = dict(row)
        row["updated_at"] = _utc_now()
        row["status"] = status
        row["broker_found"] = found
        row["last_status_check_ms"] = one_ms

        if status == "complete":
            row["completed"] = True
            row["submitted"] = True
            row["execution_price"] = exec_price
            row["message"] = "Order filled"
            row["confirmed_at"] = _utc_now()
            updates += 1
        elif status == "rejected":
            row["completed"] = False
            row["message"] = "Order rejected by broker"
            row["confirmed_at"] = _utc_now()
            updates += 1
        elif status == "cancelled":
            row["completed"] = False
            row["message"] = "Order cancelled"
            row["confirmed_at"] = _utc_now()
            updates += 1
        elif status == "pending":
            row["message"] = "Order pending at broker"
        elif status == "submitted":
            row["message"] = "Order submitted; awaiting fill confirmation"
        elif not found:
            row["message"] = "Awaiting broker order status"
        else:
            row["message"] = f"Order status: {status}"

        if status != prev_status:
            logger.info(
                "pending_order_status request_id=%s order_id=%s prev=%s now=%s check_ms=%.1f",
                row.get("request_id"),
                order_id,
                prev_status,
                status,
                one_ms,
            )
        refreshed.append(row)

    store["orders"] = refreshed[:_max_pending_orders()]
    _write_json(_pending_orders_file(), store)

    total_ms = round((time.perf_counter() - t0) * 1000, 1)
    if latency_meta is not None:
        latency_meta["pending_order_refresh_ms"] = total_ms
        latency_meta["pending_order_updates"] = updates
        latency_meta["pending_order_count"] = len(refreshed)
    logger.info(
        "pending_orders_refresh count=%s updates=%s total_ms=%.1f",
        len(refreshed),
        updates,
        total_ms,
    )
    return refreshed
