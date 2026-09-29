import json
import os
import tempfile
from datetime import datetime

from web.shared.state_store import _abs_path, _file_lock, _lock_path

RESET_FLAG_FILE = os.getenv("WEB_BROKER_SESSION_RESET_FILE", "logs/web_broker_session_reset.flag")


def _flag_path() -> str:
    return _abs_path(RESET_FLAG_FILE)


def request_broker_session_reset() -> str:
    """Signal worker (separate process) to drop cached Neo session on next poll."""
    path = _flag_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ts = datetime.now().isoformat()
    lock_path = _lock_path(path)

    with _file_lock(lock_path, shared=False):
        fd, temp_path = tempfile.mkstemp(prefix="broker_reset_", suffix=".flag", dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"requested_at": ts}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
    return ts


def consume_broker_session_reset_request() -> bool:
    """Return True once if a reset was requested; removes the flag file."""
    path = _flag_path()
    if not os.path.exists(path):
        return False

    lock_path = _lock_path(path)
    with _file_lock(lock_path, shared=False):
        if not os.path.exists(path):
            return False
        try:
            os.remove(path)
        except Exception:
            return False
    return True
