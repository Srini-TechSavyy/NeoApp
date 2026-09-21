import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from web.shared.order_status import lookup_order_status, normalize_broker_status
from web.shared.trade_orders import (
    begin_idempotent_trade,
    complete_idempotent_trade,
    register_pending_order,
    refresh_pending_orders,
)


class OrderStatusTests(unittest.TestCase):
    def test_normalize_broker_status(self):
        self.assertEqual(normalize_broker_status("complete"), "complete")
        self.assertEqual(normalize_broker_status("REJECTED"), "rejected")
        self.assertEqual(normalize_broker_status("cancelled"), "cancelled")
        self.assertEqual(normalize_broker_status("open"), "pending")

    def test_lookup_order_history_complete(self):
        client = MagicMock()
        client.order_history.return_value = {
            "data": [
                {"ordSt": "open"},
                {"ordSt": "complete", "avgPrc": "123.45"},
            ]
        }
        status, price, found = lookup_order_status(client, "111")
        self.assertTrue(found)
        self.assertEqual(status, "complete")
        self.assertEqual(price, 123.45)

    def test_lookup_order_rejected_via_report(self):
        client = MagicMock()
        client.order_history.return_value = {"data": []}
        client.order_report.return_value = {"data": [{"nOrdNo": "222", "ordSt": "rejected"}]}
        status, price, found = lookup_order_status(client, "222")
        self.assertTrue(found)
        self.assertEqual(status, "rejected")
        self.assertIsNone(price)


class TradeOrdersStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pending = os.path.join(self.tmp.name, "pending.json")
        self.idem = os.path.join(self.tmp.name, "idem.json")
        os.environ["WEB_PENDING_ORDERS_FILE"] = self.pending
        os.environ["WEB_TRADE_IDEMPOTENCY_FILE"] = self.idem

    def tearDown(self):
        self.tmp.cleanup()

    def test_idempotent_replay(self):
        cached = begin_idempotent_trade("client-abc-12345")
        self.assertIsNone(cached)
        complete_idempotent_trade(
            "client-abc-12345",
            {"ok": True, "action": "BUY", "trading_symbol": "X", "lots": 1, "broker_response": {}},
        )
        replay = begin_idempotent_trade("client-abc-12345")
        self.assertIsInstance(replay, dict)
        self.assertTrue(replay.get("ok"))

    def test_pending_order_refresh_to_complete(self):
        register_pending_order(
            request_id="req1",
            order_id="999",
            symbol="NIFTY26AUG25000CE",
            side="BUY",
            lots=1,
            client_request_id="client-abc-12345",
            action="BUY",
        )
        client = MagicMock()
        client.order_history.return_value = {"data": [{"ordSt": "complete", "avgPrc": "50.5"}]}
        updated = refresh_pending_orders(client)
        self.assertEqual(len(updated), 1)
        self.assertTrue(updated[0]["completed"])
        self.assertEqual(updated[0]["status"], "complete")
        self.assertEqual(updated[0]["execution_price"], 50.5)


if __name__ == "__main__":
    unittest.main()
