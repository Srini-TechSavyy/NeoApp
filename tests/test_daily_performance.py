import unittest
from datetime import date, datetime, timedelta

from web.backend.models import DailyTradeStats, MonitorSnapshotData, MonitorTradeStats
from web.shared.monitor_snapshot import _trade_date, build_monitor_snapshot


class TestDailyTradePerformance(unittest.TestCase):
    def test_trade_date_parsing(self):
        today = date(2026, 10, 30)
        dt = datetime(2026, 10, 30, 10, 15, 30)

        # datetime object
        self.assertEqual(_trade_date({"sell_time": dt}), today)
        self.assertEqual(_trade_date({"buy_time": dt}), today)
        self.assertEqual(_trade_date({"trade_date": dt}), today)

        # date object
        self.assertEqual(_trade_date({"trade_date": today}), today)

        # ISO string
        self.assertEqual(_trade_date({"trade_date": "2026-10-30"}), today)
        self.assertEqual(_trade_date({"sell_time": "2026-10-30T10:15:30"}), today)

        # Empty / None
        self.assertIsNone(_trade_date({}))

    def test_profitable_completed_trade_increments_wins(self):
        today = date(2026, 10, 30)
        completed_trades = [
            {
                "trade_date": today,
                "sell_time": datetime(2026, 10, 30, 10, 0),
                "gross_pnl": 150.0,
                "charges": 20.0,
                "net_pnl": 130.0,
            }
        ]

        snap = build_monitor_snapshot(completed_trades, for_date=today)
        today_stats = snap["today_trade_stats"]

        self.assertEqual(today_stats["wins"], 1)
        self.assertEqual(today_stats["losses"], 0)
        self.assertEqual(today_stats["break_even"], 0)
        self.assertEqual(today_stats["total_trades"], 1)

        # Also verify in trade_stats backwards-compatibility
        self.assertEqual(snap["trade_stats"]["today_wins"], 1)
        self.assertEqual(snap["trade_stats"]["today_losses"], 0)
        self.assertEqual(snap["trade_stats"]["today_total"], 1)

    def test_losing_completed_trade_increments_losses(self):
        today = date(2026, 10, 30)
        completed_trades = [
            {
                "trade_date": today,
                "sell_time": datetime(2026, 10, 30, 10, 5),
                "gross_pnl": -80.0,
                "charges": 20.0,
                "net_pnl": -100.0,
            }
        ]

        snap = build_monitor_snapshot(completed_trades, for_date=today)
        today_stats = snap["today_trade_stats"]

        self.assertEqual(today_stats["wins"], 0)
        self.assertEqual(today_stats["losses"], 1)
        self.assertEqual(today_stats["break_even"], 0)
        self.assertEqual(today_stats["total_trades"], 1)

    def test_break_even_trade_counted_separately(self):
        today = date(2026, 10, 30)
        completed_trades = [
            {
                "trade_date": today,
                "sell_time": datetime(2026, 10, 30, 10, 10),
                "gross_pnl": 20.0,
                "charges": 20.0,
                "net_pnl": 0.0,
            }
        ]

        snap = build_monitor_snapshot(completed_trades, for_date=today)
        today_stats = snap["today_trade_stats"]

        self.assertEqual(today_stats["wins"], 0)
        self.assertEqual(today_stats["losses"], 0)
        self.assertEqual(today_stats["break_even"], 1)
        self.assertEqual(today_stats["total_trades"], 1)

    def test_mixed_trades_calculation(self):
        today = date(2026, 10, 30)
        completed_trades = [
            {"trade_date": today, "net_pnl": 500.0},
            {"trade_date": today, "net_pnl": -250.0},
            {"trade_date": today, "net_pnl": 120.0},
            {"trade_date": today, "net_pnl": 0.0},
            {"trade_date": today, "net_pnl": -50.0},
        ]

        snap = build_monitor_snapshot(completed_trades, for_date=today)
        today_stats = snap["today_trade_stats"]

        self.assertEqual(today_stats["wins"], 2)
        self.assertEqual(today_stats["losses"], 2)
        self.assertEqual(today_stats["break_even"], 1)
        self.assertEqual(today_stats["total_trades"], 5)

    def test_multiple_partial_exits_consolidate_into_one_completed_trade(self):
        # In PositionPnLEngine, partial exits accumulate in pos until open_qty == 0.
        # This verifies that a single completed trade with multiple exits is counted once.
        today = date(2026, 10, 30)
        completed_trades = [
            {
                "trade_date": today,
                "sell_time": datetime(2026, 10, 30, 10, 30),
                "qty": 50,
                "gross_pnl": 300.0,
                "charges": 40.0,
                "net_pnl": 260.0,
            }
        ]

        snap = build_monitor_snapshot(completed_trades, for_date=today)
        self.assertEqual(snap["today_trade_stats"]["total_trades"], 1)
        self.assertEqual(snap["today_trade_stats"]["wins"], 1)

    def test_open_position_not_counted_in_completed_trades(self):
        today = date(2026, 10, 30)
        completed_trades = [
            {"trade_date": today, "net_pnl": 100.0}
        ]
        open_pos = {
            "symbol": "NIFTY26OCT25000CE",
            "qty": 50,
            "entry_price": 100.0,
            "ltp": 110.0,
            "unrealized_pnl": 500.0,
        }

        snap = build_monitor_snapshot(completed_trades, open_position=open_pos, for_date=today)
        self.assertEqual(snap["today_trade_stats"]["wins"], 1)
        self.assertEqual(snap["today_trade_stats"]["total_trades"], 1)
        self.assertEqual(snap["pnl"]["unrealized"], 500.0)

    def test_historical_trades_excluded_and_counts_reset_for_next_day(self):
        yesterday = date(2026, 10, 29)
        today = date(2026, 10, 30)
        tomorrow = date(2026, 10, 31)

        historical_trades = [
            {"trade_date": yesterday, "net_pnl": 200.0},
            {"trade_date": yesterday, "net_pnl": -100.0},
            {"trade_date": today, "net_pnl": 150.0},
            {"trade_date": today, "net_pnl": -50.0},
        ]

        # For today: should only count today's 2 trades
        snap_today = build_monitor_snapshot(historical_trades, for_date=today)
        self.assertEqual(snap_today["today_trade_stats"]["wins"], 1)
        self.assertEqual(snap_today["today_trade_stats"]["losses"], 1)
        self.assertEqual(snap_today["today_trade_stats"]["total_trades"], 2)

        # For tomorrow: counts should automatically reset to 0 without deleting history
        snap_tomorrow = build_monitor_snapshot(historical_trades, for_date=tomorrow)
        self.assertEqual(snap_tomorrow["today_trade_stats"]["wins"], 0)
        self.assertEqual(snap_tomorrow["today_trade_stats"]["losses"], 0)
        self.assertEqual(snap_tomorrow["today_trade_stats"]["break_even"], 0)
        self.assertEqual(snap_tomorrow["today_trade_stats"]["total_trades"], 0)

    def test_pydantic_model_validation(self):
        today = date(2026, 10, 30)
        completed_trades = [
            {"trade_date": today, "net_pnl": 100.0},
            {"trade_date": today, "net_pnl": -50.0},
        ]
        snap = build_monitor_snapshot(completed_trades, for_date=today)

        model = MonitorSnapshotData.model_validate(snap)
        self.assertIsNotNone(model.today_trade_stats)
        self.assertEqual(model.today_trade_stats.wins, 1)
        self.assertEqual(model.today_trade_stats.losses, 1)
        self.assertEqual(model.today_trade_stats.total_trades, 2)
        self.assertEqual(model.trade_stats.today_wins, 1)


if __name__ == "__main__":
    unittest.main()
