import importlib
import json
import os
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

# Broker SDK is optional for kill-switch API tests.
if "neo_api_client" not in sys.modules:
    sys.modules["neo_api_client"] = MagicMock()

from common.time_utils import MARKET_TZ, format_ist_datetime, next_midnight_ist_epoch
from web.shared import kill_switch
from web.shared import risk_controls


class NextMidnightIstTests(unittest.TestCase):
    def test_late_evening_points_to_next_calendar_midnight(self):
        ist = datetime(2026, 3, 15, 23, 30, 0, tzinfo=MARKET_TZ)
        epoch = next_midnight_ist_epoch(ist)
        expected = datetime(2026, 3, 16, 0, 0, 0, tzinfo=MARKET_TZ).timestamp()
        self.assertEqual(epoch, expected)
        self.assertIn("2026-03-16 00:00:00 IST", format_ist_datetime(epoch))


class KillSwitchPersistenceTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "kill_switch.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _patch_file(self):
        return patch.object(kill_switch, "KILL_SWITCH_FILE", str(self.path))

    def test_activate_writes_file_and_status_active(self):
        with self._patch_file():
            status = kill_switch.activate_kill_switch()
        self.assertTrue(status["active"])
        self.assertGreater(status["expires_at"], time.time())
        self.assertTrue(self.path.is_file())
        with self._patch_file():
            again = kill_switch.get_kill_switch_status()
        self.assertTrue(again["active"])
        self.assertEqual(again["expires_at"], status["expires_at"])

    def test_expired_purged_on_read(self):
        past = time.time() - 60
        self.path.write_text(
            json.dumps({"active": True, "activated_at": past - 3600, "expires_at": past}),
            encoding="utf-8",
        )
        with self._patch_file():
            status = kill_switch.get_kill_switch_status(purge_expired=True)
        self.assertFalse(status["active"])
        self.assertFalse(self.path.exists())

    def test_persistence_survives_module_reload(self):
        with self._patch_file():
            kill_switch.activate_kill_switch()
            reloaded = importlib.reload(kill_switch)
            with patch.object(reloaded, "KILL_SWITCH_FILE", str(self.path)):
                status = reloaded.get_kill_switch_status()
        self.assertTrue(status["active"])


class KillSwitchRiskIntegrationTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.ks_path = Path(self.tmp.name) / "kill_switch.json"
        self.buy_path = Path(self.tmp.name) / "buy_disabled.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_evaluate_risk_blocks_buy_with_kill_switch(self):
        expires = time.time() + 3600
        self.ks_path.write_text(
            json.dumps({"active": True, "activated_at": time.time(), "expires_at": expires}),
            encoding="utf-8",
        )
        with patch.object(kill_switch, "KILL_SWITCH_FILE", str(self.ks_path)), patch.object(
            risk_controls, "BUY_DISABLED_FILE", str(self.buy_path)
        ):
            risk = risk_controls.evaluate_risk_state([], last_exit_epoch=0.0, cool_off_seconds=0)
        self.assertFalse(risk["buy_allowed"])
        self.assertEqual(risk["reason"], "kill_switch")
        self.assertTrue(risk["kill_switch_active"])


class KillSwitchApiTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.ks_path = Path(self.tmp.name) / "kill_switch.json"
        os.environ["WEB_ALLOW_LOCAL_NOAUTH"] = "true"
        os.environ["WEB_API_TOKEN"] = ""
        os.environ["TRADING_ENABLED"] = "true"

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("WEB_ALLOW_LOCAL_NOAUTH", None)
        os.environ.pop("WEB_API_TOKEN", None)
        os.environ.pop("TRADING_ENABLED", None)

    def test_trade_action_buy_blocked_by_kill_switch(self):
        expires = time.time() + 7200
        self.ks_path.write_text(
            json.dumps({"active": True, "activated_at": time.time(), "expires_at": expires}),
            encoding="utf-8",
        )
        snapshot = {"risk": {"buy_allowed": True, "reason": "", "cooloff_remaining": 0}}
        from fastapi.testclient import TestClient
        from web.backend.main import app

        with patch.object(kill_switch, "KILL_SWITCH_FILE", str(self.ks_path)), patch(
            "web.backend.main.read_snapshot", return_value=snapshot
        ), patch("web.backend.main.is_trading_enabled", return_value=True):

            with TestClient(app) as client:
                resp = client.post(
                    "/api/trade/action",
                    json={
                        "trading_symbol": "NIFTY26AUG25000CE",
                        "lots": 1,
                        "action": "BUY",
                        "client_request_id": "kill-switch-test-01",
                    },
                )
        self.assertEqual(resp.status_code, 429)
        self.assertIn("kill switch", resp.json().get("detail", "").lower())

    def test_trade_action_exit_not_blocked_by_kill_switch(self):
        expires = time.time() + 7200
        self.ks_path.write_text(
            json.dumps({"active": True, "activated_at": time.time(), "expires_at": expires}),
            encoding="utf-8",
        )
        from fastapi.testclient import TestClient
        from web.backend.main import app

        with patch.object(kill_switch, "KILL_SWITCH_FILE", str(self.ks_path)), patch(
            "web.backend.main.read_snapshot", return_value={}
        ), patch("web.backend.main.is_trading_enabled", return_value=True), patch(
            "web.backend.main.execute_market_action",
            return_value={"order_id": "1", "symbol": "NIFTY26AUG25000CE", "side": "SELL", "lots": 1},
        ):

            with TestClient(app) as client:
                resp = client.post(
                    "/api/trade/action",
                    json={
                        "trading_symbol": "NIFTY26AUG25000CE",
                        "lots": 1,
                        "action": "EXIT",
                    },
                )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json().get("ok"))

    def test_kill_switch_activate_endpoint(self):
        from fastapi.testclient import TestClient
        from web.backend.main import app

        with patch.object(kill_switch, "KILL_SWITCH_FILE", str(self.ks_path)):
            with TestClient(app) as client:
                resp = client.post("/api/kill-switch/activate")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body.get("active"))
        self.assertIn("IST", body.get("expires_at_ist", ""))


class KillSwitchFrontendTests(unittest.TestCase):
    def test_index_html_includes_kill_switch_controls(self):
        html = Path("web/backend/static/index.html").read_text(encoding="utf-8")
        self.assertIn("killSwitchToggle", html)
        self.assertIn("/api/kill-switch", html)
        self.assertIn("syncTradeButtons", html)


if __name__ == "__main__":
    unittest.main()
