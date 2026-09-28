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


if __name__ == "__main__":
    unittest.main()
