#!/usr/bin/env python3
"""Start NeoApp's worker and FastAPI backend in one container."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
PORT = os.getenv("PORT", "8080")
PYTHON = sys.executable


def _bootstrap_env() -> Dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PORT", PORT)
    return env


def _spawn(command: List[str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        command,
        cwd=str(ROOT),
        env=_bootstrap_env(),
    )


def _terminate_process(proc: Optional[subprocess.Popen[str]], name: str, timeout: float = 20.0) -> None:
    if proc is None or proc.poll() is not None:
        return

    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"[cloudflare] {name} did not exit in time; killing it.", flush=True)
        proc.kill()
        proc.wait(timeout=timeout)


def _start_worker() -> subprocess.Popen[str]:
    print("[cloudflare] starting NeoApp worker", flush=True)
    return _spawn([PYTHON, "-m", "web.worker.main"])


def _start_web() -> subprocess.Popen[str]:
    print(f"[cloudflare] starting FastAPI backend on 0.0.0.0:{PORT}", flush=True)
    return _spawn(
        [
            PYTHON,
            "-m",
            "uvicorn",
            "web.backend.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            PORT,
        ]
    )


def main() -> int:
    (ROOT / "logs").mkdir(exist_ok=True)

    worker = _start_worker()
    web = _start_web()
    shutting_down = False
    restart_delay_seconds = 5

    def handle_signal(signum, _frame):
        nonlocal shutting_down
        shutting_down = True
        print(f"[cloudflare] received signal {signum}; stopping child processes", flush=True)
        _terminate_process(web, "uvicorn")
        _terminate_process(worker, "worker")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        while True:
            web_exit = web.poll()
            if web_exit is not None:
                print(f"[cloudflare] uvicorn exited with code {web_exit}", flush=True)
                shutting_down = True
                _terminate_process(worker, "worker")
                return int(web_exit)

            worker_exit = worker.poll()
            if worker_exit is not None:
                print(
                    f"[cloudflare] worker exited with code {worker_exit}; restarting in {restart_delay_seconds}s",
                    flush=True,
                )
                _terminate_process(worker, "worker")
                if shutting_down:
                    return int(worker_exit)
                time.sleep(restart_delay_seconds)
                worker = _start_worker()
                restart_delay_seconds = min(restart_delay_seconds * 2, 30)
                continue

            time.sleep(1)
    finally:
        _terminate_process(web, "uvicorn")
        _terminate_process(worker, "worker")


if __name__ == "__main__":
    raise SystemExit(main())