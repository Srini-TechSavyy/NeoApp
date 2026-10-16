import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


class SymbolSuggestApiTests(unittest.TestCase):
    def setUp(self):
        os.environ["WEB_ALLOW_LOCAL_NOAUTH"] = "true"
        os.environ["WEB_API_TOKEN"] = ""

    @patch("web.backend.main.suggest_option_symbol")
    def test_suggest_success(self, mock_suggest):
        mock_suggest.return_value = "NIFTY26OCT24500CE"
        with patch(
            "web.backend.main.validate_trading_symbol",
            return_value={
                "ok": True,
                "expiry_token": "26OCT",
                "instrument_token": "51418",
            },
        ):
            from web.backend.main import app

            with TestClient(app) as client:
                resp = client.post(
                    "/api/symbol/suggest",
                    json={
                        "base_symbol": "NIFTY",
                        "index_ltp": 24500,
                        "option_type": "CE",
                        "offset": 0,
                    },
                )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("symbol"), "NIFTY26OCT24500CE")
        self.assertEqual(body.get("expiry"), "26OCT")
        self.assertEqual(body.get("instrument_token"), "51418")

    @patch("web.backend.main.suggest_option_symbol")
    def test_suggest_missing_master_returns_503_not_500(self, mock_suggest):
        mock_suggest.side_effect = FileNotFoundError("No valid scrip master files found.")
        from web.backend.main import app

        with TestClient(app) as client:
            resp = client.post(
                "/api/symbol/suggest",
                json={
                    "base_symbol": "NIFTY",
                    "index_ltp": 24500,
                    "option_type": "CE",
                    "offset": 0,
                },
            )
        self.assertEqual(resp.status_code, 503)
        self.assertIn("unavailable", resp.json().get("detail", "").lower())

    def test_missing_holiday_file_does_not_crash_calendar(self):
        from common.nse_calendar import is_exchange_holiday, reload_holiday_calendar

        with patch("common.nse_calendar.get_resource_path", return_value="/nonexistent/holidays.json"):
            reload_holiday_calendar()
            self.assertFalse(is_exchange_holiday(__import__("datetime").date(2026, 3, 3)))
        reload_holiday_calendar()


if __name__ == "__main__":
    unittest.main()
