"""
3 completed 1-minute candle signal logic for the web worker.

Persistent signal_direction: UI background / trend display (stays until opposite 3-candle confirm).
signal_event: one-shot entry trigger (only when bullish_streak==3 or bearish_streak==3 on a close).
"""

from datetime import datetime
from typing import List, Optional

import pandas as pd

from common.minute_candles import update_minute_candle
from indicator.scalping_indicator import BaseStrategy

CONFIRM_COUNT = 3


def candle_color(candle: dict) -> str:
    o, c = float(candle["open"]), float(candle["close"])
    if c > o:
        return "green"
    if c < o:
        return "red"
    return "doji"


class ThreeCandleSignalState:
    """Evaluates only completed 1-minute candles."""

    def __init__(self):
        self.bullish_streak = 0
        self.bearish_streak = 0
        # Persistent display / trend state
        self.signal_direction = "NEUTRAL"
        self.last_confirmed_signal = "NEUTRAL"
        # One-shot event latched until consumed by get_signal()
        self._pending_event: Optional[str] = None

    def on_closed_candle(self, candle: dict) -> None:
        color = candle_color(candle)
        event = "NONE"

        if color == "doji":
            self.bullish_streak = 0
            self.bearish_streak = 0
        elif color == "green":
            self.bullish_streak += 1
            self.bearish_streak = 0
            if self.bullish_streak == CONFIRM_COUNT:
                self.signal_direction = "BULLISH"
                self.last_confirmed_signal = "BULLISH"
                event = "BULLISH"
        else:
            self.bearish_streak += 1
            self.bullish_streak = 0
            if self.bearish_streak == CONFIRM_COUNT:
                self.signal_direction = "BEARISH"
                self.last_confirmed_signal = "BEARISH"
                event = "BEARISH"

        if event != "NONE":
            self._pending_event = event

    def consume_pending_event(self) -> str:
        ev = self._pending_event or "NONE"
        self._pending_event = None
        return ev

    @property
    def candle_streak(self) -> int:
        if self.bullish_streak > 0:
            return self.bullish_streak
        if self.bearish_streak > 0:
            return self.bearish_streak
        return 0

    def payload_base(self) -> dict:
        return {
            "signal": self.signal_direction,
            "signal_direction": self.signal_direction,
            "last_confirmed_signal": self.last_confirmed_signal,
            "candle_streak": self.candle_streak,
            "ema": 0,
            "roc": 0,
            "bb_width": 0,
            "rsi": 0,
            "momentum": 0,
            "pulse": False,
            "exhausted": False,
            "sideways": False,
            "trend": "FLAT",
        }


class ThreeCandleMinuteStrategy(BaseStrategy):
    """Web worker strategy: 1-minute OHLC + 3-candle confirmation."""

    def __init__(self):
        self._candles: List[dict] = []
        self._state = ThreeCandleSignalState()
        self._symbol = ""

    def on_ltp(self, ltp: float, symbol: str):
        self._symbol = str(symbol or "").strip().upper()
        closed = update_minute_candle(self._candles, float(ltp), now=datetime.now())
        if closed and len(self._candles) >= 2:
            self._state.on_closed_candle(self._candles[-2])

    def get_signal(self, history: pd.DataFrame, consume_event: bool = True) -> dict:
        # history unused; tick CSV may still be written by LiveScalpingManager
        out = self._state.payload_base()
        if consume_event:
            out["signal_event"] = self._state.consume_pending_event()
        else:
            out["signal_event"] = self._state._pending_event or "NONE"
        if len(history):
            out["ltp"] = float(history["ltp"].iloc[-1])
        return out

    # Test helpers
    def reset(self):
        self._candles = []
        self._state = ThreeCandleSignalState()

    def feed_ltp(self, ltp: float, now: datetime):
        """Inject LTP at a fixed time (unit tests)."""
        closed = update_minute_candle(self._candles, float(ltp), now=now)
        if closed and len(self._candles) >= 2:
            self._state.on_closed_candle(self._candles[-2])

    @property
    def state(self) -> ThreeCandleSignalState:
        return self._state
