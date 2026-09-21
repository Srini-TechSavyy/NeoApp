"""Temporary structured latency logging for BUY/EXIT trade actions."""

import logging
import time
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger("neoapp.trade.latency")


class TradeLatencyRecorder:
    """Records per-stage deltas (ms) for a single trade request."""

    def __init__(self, request_id: Optional[str] = None) -> None:
        self.request_id = request_id or uuid.uuid4().hex[:12]
        self.stages: Dict[str, float] = {}
        self.meta: Dict[str, Any] = {}
        self._t0 = time.perf_counter()
        self._last = self._t0

    def mark(self, stage: str) -> None:
        now = time.perf_counter()
        self.stages[stage] = round((now - self._last) * 1000, 1)
        self._last = now

    def add_meta(self, **kwargs: Any) -> None:
        self.meta.update(kwargs)

    def total_ms(self) -> float:
        return round((time.perf_counter() - self._t0) * 1000, 1)

    def flush(self, action: str = "", symbol: str = "") -> None:
        logger.info(
            "trade_latency request_id=%s action=%s symbol=%s total_ms=%.1f stages=%s meta=%s",
            self.request_id,
            action,
            symbol,
            self.total_ms(),
            self.stages,
            self.meta,
        )


def latency_enabled() -> bool:
    import os

    return os.getenv("TRADE_LATENCY_LOG", "true").strip().lower() in ("1", "true", "yes", "on")
