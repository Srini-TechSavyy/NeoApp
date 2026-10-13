import json
import os
import time
from typing import Any, Dict

from common.time_utils import format_ist_datetime, next_midnight_ist_epoch

KILL_SWITCH_FILE = os.getenv("KILL_SWITCH_FILE", "kill_switch.json")


def _abs_path(path: str) -> str:
    if os.path.isabs(path):
        return path
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, path)


def _load_raw() -> Dict[str, Any]:
    path = _abs_path(KILL_SWITCH_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_raw(data: Dict[str, Any]) -> None:
    path = _abs_path(KILL_SWITCH_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _clear_file() -> None:
    path = _abs_path(KILL_SWITCH_FILE)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _status_from_data(data: Dict[str, Any], active: bool) -> Dict[str, Any]:
    expires_at = float(data.get("expires_at", 0) or 0)
    activated_at = float(data.get("activated_at", 0) or 0)
    return {
        "active": active,
        "expires_at": expires_at if active else 0.0,
        "expires_at_ist": format_ist_datetime(expires_at) if active and expires_at > 0 else "",
        "activated_at": activated_at if active else 0.0,
    }


def get_kill_switch_status(purge_expired: bool = True) -> Dict[str, Any]:
    data = _load_raw()
    if not data.get("active"):
        return _status_from_data({}, False)

    expires_at = float(data.get("expires_at", 0) or 0)
    now = time.time()
    if expires_at <= 0 or (purge_expired and now >= expires_at):
        _clear_file()
        return _status_from_data({}, False)

    return _status_from_data(data, True)


def activate_kill_switch() -> Dict[str, Any]:
    existing = get_kill_switch_status(purge_expired=True)
    if existing.get("active"):
        return existing

    now = time.time()
    expires_at = next_midnight_ist_epoch()
    payload = {
        "active": True,
        "activated_at": now,
        "expires_at": expires_at,
    }
    _save_raw(payload)
    return _status_from_data(payload, True)


def is_buy_blocked_by_kill_switch() -> Dict[str, Any]:
    """Returns status dict; use status['active'] for block decision."""
    return get_kill_switch_status(purge_expired=True)
