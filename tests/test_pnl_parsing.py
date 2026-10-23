import unittest
from datetime import datetime

from monitor.pnl_engine import (
    PositionPnLEngine,
    parse_api_fills,
    parse_api_orders,
    broker_trades_for_pnl,
)


class TestPnLParsing(unittest.TestCase):
    def test_parse_traded_status_order(self):
        rows = [
            {
                "ordSt": "traded",
                "trdSym": "NIFTY26SEP22800PE",
                "trnsTp": "B",
                "fldQty": 65,
                "avgPrc": 10.5,
                "exCfmTm": "28-Sep-2026 09:15:00",
                "exSeg": "nse_fo",
                "prod": "MIS",
                "nOrdNo": "1",
            }
        ]
        trades = parse_api_orders(rows)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].side, "B")
        self.assertEqual(trades[0].qty, 65)

    def test_parse_trade_report_fill(self):
        rows = [
            {
                "trdSym": "NIFTY26SEP22800PE",
                "trnsTp": "B",
                "fldQty": 65,
                "avgPrc": 10.0,
                "hsUpTm": "2026/09/28 09:15:00",
                "exSeg": "nse_fo",
                "nOrdNo": "1",
            },
            {
                "trdSym": "NIFTY26SEP22800PE",
                "trnsTp": "S",
                "fldQty": 65,
                "avgPrc": 12.0,
                "hsUpTm": "2026/09/28 09:20:00",
                "exSeg": "nse_fo",
                "nOrdNo": "2",
            },
        ]
        trades = parse_api_fills(rows)
        self.assertEqual(len(trades), 2)
        engine = PositionPnLEngine()
        for t in sorted(trades, key=lambda x: x.time):
            engine.add_trade(t)
        self.assertEqual(len(engine.completed_trades), 1)
        self.assertGreater(engine.completed_trades[0]["net_pnl"], 0)

    def test_broker_trades_prefers_order_report(self):
        class FakeClient:
            def order_report(self):
                return {
                    "data": [
                        {
                            "ordSt": "complete",
                            "trdSym": "NIFTY26SEP22800PE",
                            "trnsTp": "B",
                            "fldQty": 10,
                            "avgPrc": 5.0,
                            "exCfmTm": "28-Sep-2026 09:15:00",
                            "exSeg": "nse_fo",
                            "nOrdNo": "1",
                        },
                        {
                            "ordSt": "complete",
                            "trdSym": "NIFTY26SEP22800PE",
                            "trnsTp": "S",
                            "fldQty": 10,
                            "avgPrc": 6.0,
                            "exCfmTm": "28-Sep-2026 09:16:00",
                            "exSeg": "nse_fo",
                            "nOrdNo": "2",
                        },
                    ]
                }

            def trade_report(self):
                return {"data": []}

        trades, meta = broker_trades_for_pnl(FakeClient())
        self.assertEqual(meta["source"], "order_report")
        self.assertEqual(len(trades), 2)

    def test_broker_trades_fallback_to_trade_report(self):
        class FakeClient:
            def order_report(self):
                return {"data": []}

            def trade_report(self):
                return {
                    "data": [
                        {
                            "trdSym": "NIFTY26SEP22800PE",
                            "trnsTp": "B",
                            "fldQty": 10,
                            "avgPrc": 5.0,
                            "hsUpTm": "2026/09/28 09:15:00",
                            "exSeg": "nse_fo",
                            "nOrdNo": "1",
                        },
                        {
                            "trdSym": "NIFTY26SEP22800PE",
                            "trnsTp": "S",
                            "fldQty": 10,
                            "avgPrc": 6.0,
                            "hsUpTm": "2026/09/28 09:16:00",
                            "exSeg": "nse_fo",
                            "nOrdNo": "2",
                        },
                    ]
                }

        trades, meta = broker_trades_for_pnl(FakeClient())
        self.assertEqual(meta["source"], "trade_report")
        self.assertEqual(len(trades), 2)

    def test_broker_trades_skips_trade_report_when_orders_complete(self):
        called = {"trade_report": False}

        class FakeClient:
            def order_report(self):
                return {
                    "data": [
                        {
                            "ordSt": "complete",
                            "trdSym": "NIFTY26SEP22800PE",
                            "trnsTp": "B",
                            "fldQty": 10,
                            "avgPrc": 5.0,
                            "exCfmTm": "28-Sep-2026 09:15:00",
                            "exSeg": "nse_fo",
                            "nOrdNo": "1",
                        },
                        {
                            "ordSt": "complete",
                            "trdSym": "NIFTY26SEP22800PE",
                            "trnsTp": "S",
                            "fldQty": 10,
                            "avgPrc": 6.0,
                            "exCfmTm": "28-Sep-2026 09:16:00",
                            "exSeg": "nse_fo",
                            "nOrdNo": "2",
                        },
                    ]
                }

            def trade_report(self):
                called["trade_report"] = True
                return {"data": []}

        trades, meta = broker_trades_for_pnl(FakeClient())
        self.assertEqual(meta["source"], "order_report")
        self.assertEqual(len(trades), 2)
        self.assertFalse(called["trade_report"], "trade_report should not be called when order_report has closed trades")

    def test_build_monitor_snapshot_with_open_position_unrealized_pnl(self):
        from web.shared.monitor_snapshot import build_monitor_snapshot

        completed_trades = [
            {
                "buy_time": datetime.now(),
                "sell_time": datetime.now(),
                "gross_pnl": 100.0,
                "net_pnl": 80.0,
                "charges": 20.0,
            }
        ]
        open_pos = {
            "symbol": "NIFTY26OCT25000CE",
            "qty": 50,
            "entry_price": 100.0,
            "ltp": 105.0,
            "points": 5.0,
            "unrealized_pnl": 250.0,
        }

        snap = build_monitor_snapshot(completed_trades, open_position=open_pos)
        self.assertEqual(snap["pnl"]["gross"], 100.0)
        self.assertEqual(snap["pnl"]["net"], 80.0)
        self.assertEqual(snap["pnl"]["unrealized"], 250.0)
        self.assertEqual(snap["pnl"]["total_gross"], 350.0)
        self.assertEqual(snap["pnl"]["total_net"], 330.0)


if __name__ == "__main__":
    unittest.main()
