import os
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from web.shared.index_quotes import fetch_index_ltp, fetch_index_ltp_for_base


class IndexQuotesTests(unittest.TestCase):
    def test_fetch_index_ltp_list_payload(self):
        client = MagicMock()
        client.quotes.return_value = [{"ltp": 23456.75}]
        ltp, token, exch, name = fetch_index_ltp(client, "NIFTY")
        self.assertEqual(ltp, 23456.75)
        self.assertEqual(name, "NIFTY")
        self.assertEqual(exch, "nse_cm")

    def test_fetch_index_ltp_for_base_rejects_unknown(self):
        with self.assertRaises(ValueError):
            fetch_index_ltp_for_base("BANKNIFTY")

    @patch("common.orders.ensure_login")
    def test_fetch_index_ltp_for_base_success(self, mock_login):
        broker = MagicMock()
        broker.quotes.return_value = {"data": [{"ltp": 81234.5}]}
        mock_login.return_value = broker
        ltp, name = fetch_index_ltp_for_base("SENSEX")
        self.assertEqual(ltp, 81234.5)
        self.assertEqual(name, "SENSEX")


class IndexLtpApiTests(unittest.TestCase):
    def test_index_ltp_endpoint(self):
        os.environ["WEB_ALLOW_LOCAL_NOAUTH"] = "true"
        os.environ["WEB_API_TOKEN"] = ""
        with patch("web.backend.main.fetch_index_ltp_for_base", return_value=(25000.25, "Nifty 50")):
            from web.backend.main import app

            with TestClient(app) as client:
                resp = client.get("/api/index/ltp", params={"base_symbol": "NIFTY"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("base_symbol"), "NIFTY")
        self.assertEqual(body.get("index_ltp"), 25000.25)


if __name__ == "__main__":
    unittest.main()
