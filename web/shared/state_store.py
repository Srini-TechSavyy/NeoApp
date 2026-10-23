import json
import os
import tempfile
from contextlib import contextmanager
from typing import Any, Dict

try:
    import fcntl
except Exception:  # pragma: no cover
    fcntl = None

STATE_FILE = os.getenv("WEB_STATE_FILE", "logs/web_monitor_state.json")
LOCK_FILE = os.getenv("WEB_STATE_LOCK_FILE", "")


def _abs_path(path: str) -> str:
    if os.path.isabs(path):
        return path
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, path)


def _lock_path(state_path: str) -> str:
    if LOCK_FILE.strip():
        return _abs_path(LOCK_FILE)
    return f"{state_path}.lock"


@contextmanager
def _file_lock(path: str, shared: bool):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+", encoding="utf-8") as lock_f:
        if fcntl:
            mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            fcntl.flock(lock_f.fileno(), mode)
        try:
            yield
        finally:
            if fcntl:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)


_cached_snapshot: Dict[str, Any] = {}
_cached_mtime_ns: int = 0


def read_snapshot() -> Dict[str, Any]:
    global _cached_snapshot, _cached_mtime_ns
    path = _abs_path(STATE_FILE)
    if not os.path.exists(path):
        return {}

    try:
        stat_res = os.stat(path)
        if stat_res.st_mtime_ns == _cached_mtime_ns and _cached_snapshot:
            return _cached_snapshot
    except OSError:
        pass

    lock_path = _lock_path(path)
    with _file_lock(lock_path, shared=True):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                _cached_snapshot = data
                try:
                    _cached_mtime_ns = os.stat(path).st_mtime_ns
                except OSError:
                    pass
                return data
        except (json.JSONDecodeError, OSError):
            # Corrupted/partial read should not crash API callers.
            return _cached_snapshot or {}


def write_snapshot(snapshot: Dict[str, Any]) -> None:
    global _cached_snapshot, _cached_mtime_ns
    path = _abs_path(STATE_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock_path = _lock_path(path)

    with _file_lock(lock_path, shared=False):
        fd, temp_path = tempfile.mkstemp(prefix="snapshot_", suffix=".json", dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, path)
            _cached_snapshot = snapshot
            try:
                _cached_mtime_ns = os.stat(path).st_mtime_ns
            except OSError:
                pass
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
