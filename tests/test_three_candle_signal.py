"""Unit tests for 3 completed 1-minute candle signal logic (no broker)."""

from datetime import datetime, timedelta

import pandas as pd

from common.minute_candles import completed_candles, update_minute_candle
from indicator.three_candle_signal import (
    ThreeCandleMinuteStrategy,
    ThreeCandleSignalState,
    candle_color,
)


def _close_candle(strategy: ThreeCandleMinuteStrategy, minute_start: datetime, open_p: float, close_p: float):
    """Simulate one completed minute; first tick in bucket sets open."""
    strategy.feed_ltp(open_p, minute_start.replace(second=2))
    strategy.feed_ltp(close_p, minute_start.replace(second=50))
    strategy.feed_ltp(close_p, minute_start + timedelta(minutes=1, seconds=2))


def _close_green(strategy: ThreeCandleMinuteStrategy, base: datetime, minute_offset: int, open_p: float, close_p: float):
    _close_candle(strategy, base + timedelta(minutes=minute_offset), open_p, close_p)


def _close_red(strategy: ThreeCandleMinuteStrategy, base: datetime, minute_offset: int, open_p: float, close_p: float):
    _close_candle(strategy, base + timedelta(minutes=minute_offset), open_p, close_p)


def _close_doji(strategy: ThreeCandleMinuteStrategy, base: datetime, minute_offset: int, price: float):
    _close_candle(strategy, base + timedelta(minutes=minute_offset), price, price)


def test_candle_color_and_doji():
    assert candle_color({"open": 100, "close": 101}) == "green"
    assert candle_color({"open": 101, "close": 100}) == "red"
    assert candle_color({"open": 100, "close": 100}) == "doji"


def test_three_green_bullish_event_and_persistent():
    s = ThreeCandleMinuteStrategy()
    base = datetime(2026, 1, 15, 9, 30, 0)
    for m in range(3):
        _close_green(s, base, m, 100 + m, 101 + m)
    out = s.get_signal(pd.DataFrame())
    assert out["signal_event"] == "BULLISH"
    assert out["signal_direction"] == "BULLISH"
    assert s.get_signal(pd.DataFrame())["signal_event"] == "NONE"
    assert s.get_signal(pd.DataFrame())["signal_direction"] == "BULLISH"


def test_three_red_bearish():
    st = ThreeCandleSignalState()
    for o, c in [(101, 100), (100, 99), (99, 98)]:
        st.on_closed_candle({"open": o, "close": c})
    assert st.signal_direction == "BEARISH"
    assert st.consume_pending_event() == "BEARISH"


def test_mixed_no_confirmation():
    st = ThreeCandleSignalState()
    st.on_closed_candle({"open": 100, "close": 101})
    st.on_closed_candle({"open": 101, "close": 102})
    st.on_closed_candle({"open": 102, "close": 101})
    assert st.signal_direction == "NEUTRAL"
    assert st.consume_pending_event() == "NONE"


def test_doji_breaks_sequence():
    st = ThreeCandleSignalState()
    st.on_closed_candle({"open": 100, "close": 101})
    st.on_closed_candle({"open": 101, "close": 102})
    st.on_closed_candle({"open": 102, "close": 102})
    st.on_closed_candle({"open": 102, "close": 103})
    assert st.bullish_streak == 1
    assert st.consume_pending_event() == "NONE"


def test_fifth_green_single_event():
    s = ThreeCandleMinuteStrategy()
    base = datetime(2026, 1, 15, 10, 0, 0)
    events = []
    for m in range(5):
        _close_green(s, base, m, 100 + m, 101 + m)
        ev = s.get_signal(pd.DataFrame(), consume_event=True)["signal_event"]
        events.append(ev)
    assert events == ["NONE", "NONE", "BULLISH", "NONE", "NONE"]


def test_green_to_red_transition():
    st = ThreeCandleSignalState()
    for o, c in [(100, 101), (101, 102), (102, 103)]:
        st.on_closed_candle({"open": o, "close": c})
    assert st.consume_pending_event() == "BULLISH"
    assert st.signal_direction == "BULLISH"
    for o, c in [(103, 102), (102, 101), (101, 100)]:
        st.on_closed_candle({"open": o, "close": c})
    assert st.consume_pending_event() == "BEARISH"
    assert st.signal_direction == "BEARISH"


def test_background_persistence_partial_reds():
    st = ThreeCandleSignalState()
    for _ in range(3):
        st.on_closed_candle({"open": 100, "close": 101})
    assert st.signal_direction == "BULLISH"
    st.consume_pending_event()
    st.on_closed_candle({"open": 101, "close": 100})
    st.on_closed_candle({"open": 100, "close": 99})
    assert st.signal_direction == "BULLISH"
    assert st.consume_pending_event() == "NONE"


def test_incomplete_candle_not_evaluated():
    candles = []
    t0 = datetime(2026, 1, 15, 9, 30, 0)
    assert update_minute_candle(candles, 100.0, t0.replace(second=10)) is False
    assert update_minute_candle(candles, 101.0, t0.replace(second=50)) is False
    assert len(candles) == 1
    assert completed_candles(candles) == []


def test_stale_persistent_no_repeat_event_without_new_triple():
    s = ThreeCandleMinuteStrategy()
    base = datetime(2026, 1, 15, 12, 0, 0)
    for m in range(3):
        _close_green(s, base, m, 100 + m, 101 + m)
    assert s.get_signal(pd.DataFrame())["signal_event"] == "BULLISH"
    for _ in range(3):
        assert s.get_signal(pd.DataFrame())["signal_event"] == "NONE"
    assert s.state.signal_direction == "BULLISH"


def test_update_minute_closes_on_rollover():
    candles = []
    t = datetime(2026, 1, 15, 9, 30, 0)
    assert update_minute_candle(candles, 100.0, t) is False
    assert update_minute_candle(candles, 101.0, t + timedelta(minutes=1)) is True
    assert len(candles) == 2
    assert candles[0]["close"] == 100.0
    assert completed_candles(candles) == [candles[0]]
