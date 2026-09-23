import re
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import web.shared.trading_actions as trading_actions
from web.backend.models import TradeActionRequest


def _reset_symbols_loaded(flag: bool = False) -> None:
    trading_actions._SYMBOLS_LOADED = flag


class ScripMasterPreloadTests(unittest.TestCase):
    def setUp(self):
        _reset_symbols_loaded(False)

    def tearDown(self):
        _reset_symbols_loaded(False)

    def test_preload_success_marks_loaded(self):
        with patch.object(trading_actions, "load_scrip_master_csv") as mock_load:
            ok = trading_actions.preload_scrip_master()
        self.assertTrue(ok)
        self.assertTrue(trading_actions.is_scrip_master_loaded())
        mock_load.assert_called_once()

    def test_preload_failure_does_not_raise_and_leaves_lazy_path(self):
        with patch.object(
            trading_actions,
            "load_scrip_master_csv",
            side_effect=FileNotFoundError("missing csv"),
        ):
            ok = trading_actions.preload_scrip_master()
        self.assertFalse(ok)
        self.assertFalse(trading_actions.is_scrip_master_loaded())

        with patch.object(trading_actions, "load_scrip_master_csv") as mock_load:
            trading_actions._ensure_symbols_loaded()
        self.assertTrue(trading_actions.is_scrip_master_loaded())
        mock_load.assert_called_once()

    def test_lazy_load_still_works_after_failed_preload(self):
        with patch.object(
            trading_actions,
            "load_scrip_master_csv",
            side_effect=RuntimeError("startup fail"),
        ):
            self.assertFalse(trading_actions.preload_scrip_master())

        calls = []

        def _load():
            calls.append(1)

        with patch.object(trading_actions, "load_scrip_master_csv", side_effect=_load):
            trading_actions._ensure_symbols_loaded()
            trading_actions._ensure_symbols_loaded()
        self.assertEqual(len(calls), 1)
        self.assertTrue(trading_actions.is_scrip_master_loaded())

    def test_concurrent_loads_call_loader_once(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def slow_load():
            calls.append(threading.current_thread().name)
            started.set()
            release.wait(timeout=2)
            time.sleep(0.05)

        with patch.object(trading_actions, "load_scrip_master_csv", side_effect=slow_load):
            threads = [
                threading.Thread(target=trading_actions._ensure_symbols_loaded)
                for _ in range(8)
            ]
            for t in threads:
                t.start()
            self.assertTrue(started.wait(timeout=2))
            release.set()
            for t in threads:
                t.join(timeout=2)

        self.assertEqual(len(calls), 1)
        self.assertTrue(trading_actions.is_scrip_master_loaded())

    def test_app_starts_when_preload_fails(self):
        with patch(
            "web.backend.main.preload_scrip_master",
            side_effect=RuntimeError("preload boom"),
        ):
            from web.backend.main import app

            with TestClient(app) as client:
                resp = client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json().get("ok"))

    def test_app_starts_when_preload_times_out(self):
        with patch("web.backend.main.SCRIP_MASTER_PRELOAD_TIMEOUT_SECONDS", 0.05), patch(
            "web.backend.main.preload_scrip_master",
            side_effect=lambda: time.sleep(1.0),
        ):
            from web.backend.main import app

            with TestClient(app) as client:
                resp = client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json().get("ok"))


class ScripMasterSessionReuseTests(unittest.TestCase):
    def test_download_reuses_existing_broker_session(self):
        import common.scrip_master as sm

        existing = MagicMock()
        existing.access_token = "tok"
        existing.scrip_master.return_value = "https://example.test/nse_fo.csv"

        with patch.object(sm, "NSE_SCRIP_MASTER_PATH", "/tmp/neo_missing_nse_fo.csv"), patch(
            "common.orders.get_client", return_value=existing
        ) as mock_get_client, patch(
            "common.orders.ensure_login"
        ) as mock_ensure_login, patch(
            "common.scrip_master.requests.get"
        ) as mock_get, patch("os.path.exists", return_value=False), patch(
            "pandas.read_csv", side_effect=FileNotFoundError("skip load")
        ):
            mock_get.return_value = MagicMock(status_code=500)
            try:
                sm.load_scrip_master_csv(paths=[])
            except FileNotFoundError:
                pass

        mock_get_client.assert_called()
        mock_ensure_login.assert_not_called()
        existing.scrip_master.assert_any_call(exchange_segment="nse_fo")
        existing.scrip_master.assert_any_call(exchange_segment="bse_fo")

    def test_download_refreshes_only_stale_segments(self):
        import common.scrip_master as sm

        existing = MagicMock()
        existing.access_token = "tok"
        existing.scrip_master.return_value = "https://example.test/bse_fo.csv"

        def exists(path):
            return path == "/tmp/neo_fresh_nse_fo.csv"

        def mtime(path):
            self.assertEqual(path, "/tmp/neo_fresh_nse_fo.csv")
            return time.time()

        with patch.object(sm, "NSE_SCRIP_MASTER_PATH", "/tmp/neo_fresh_nse_fo.csv"), patch.object(
            sm, "BSE_SCRIP_MASTER_PATH", "/tmp/neo_stale_bse_fo.csv"
        ), patch(
            "common.orders.get_client", return_value=existing
        ), patch(
            "common.orders.ensure_login"
        ) as mock_ensure_login, patch(
            "common.scrip_master.requests.get"
        ) as mock_get, patch("os.path.exists", side_effect=exists), patch(
            "os.path.getmtime", side_effect=mtime
        ), patch("pandas.read_csv", side_effect=FileNotFoundError("skip load")):
            mock_get.return_value = MagicMock(status_code=500, content=b"")
            try:
                sm.load_scrip_master_csv(paths=[])
            except FileNotFoundError:
                pass

        mock_ensure_login.assert_not_called()
        existing.scrip_master.assert_called_once_with(exchange_segment="bse_fo")


class TradePayloadAndFrontendTests(unittest.TestCase):
    def test_trade_action_request_payload_fields_unchanged(self):
        req = TradeActionRequest(
            trading_symbol="NIFTY26AUG25000CE",
            lots=2,
            action="BUY",
            client_request_id="client-req-12345",
        )
        payload = req.model_dump()
        self.assertEqual(
            set(payload.keys()),
            {"trading_symbol", "lots", "action", "client_request_id"},
        )
        self.assertEqual(payload["trading_symbol"], "NIFTY26AUG25000CE")
        self.assertEqual(payload["lots"], 2)
        self.assertEqual(payload["action"], "BUY")

        exit_req = TradeActionRequest(
            trading_symbol="NIFTY26AUG25000CE",
            lots=1,
            action="EXIT",
        )
        self.assertEqual(exit_req.action, "EXIT")

    def test_frontend_does_not_auto_suggest_before_buy_or_exit(self):
        html = Path("web/backend/static/index.html").read_text(encoding="utf-8")
        match = re.search(
            r"async function executeTrade\(action\) \{(.*?)\n    \}",
            html,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "executeTrade() not found in index.html")
        body = match.group(1)

        # Previous antipattern: always suggest immediately before BUY
        self.assertNotIn("action === 'BUY' && getIndexLtpValue()", body)
        self.assertNotRegex(
            body,
            r"if\s*\(\s*action\s*===\s*['\"]BUY['\"].*suggestSymbol",
        )

        # Suggest only when symbol is missing/invalid (and LTP is available)
        self.assertRegex(
            body,
            r"\(\s*!trading_symbol\s*\|\|\s*trading_symbol\.length\s*<\s*5\s*\)"
            r"\s*&&\s*getIndexLtpValue\(\)\s*>\s*0",
        )
        self.assertIn("await suggestSymbol()", body)
        self.assertIn("Cannot resolve trading symbol", body)

        # Explicit Suggest button binding still present
        self.assertIn("bindClick(suggestBtn, () => { suggestSymbol(); });", html)
        self.assertIn("async function fetchIndexLtpForSelectedIndex", html)
        self.assertIn("/api/index/ltp", html)

        # Trade payload shape unchanged
        self.assertIn(
            "JSON.stringify({ trading_symbol, lots, action, client_request_id })",
            html,
        )

    def test_frontend_refreshes_atm_strike_from_live_ltp(self):
        html = Path("web/backend/static/index.html").read_text(encoding="utf-8")
        self.assertIn("function expectedAtmStrike(ltp, symbolHint, optionType, offset)", html)
        self.assertIn("function refreshAtmStrikeFromLtp(ltp, positionOpen)", html)
        self.assertIn("tradeSymbolEl.value = updateSymbolStrike(cur, expected)", html)
        self.assertIn("refreshAtmStrikeFromLtp(liveLtp, positionOpen)", html)
        self.assertIn("if (tradeSymbolEl && tradeSymbolEl.matches(':focus')) return;", html)
        self.assertIn("if (positionOpen) return;", html)

        render_match = re.search(r"function render\(payload\) \{(.*?)\n    \}", html, re.DOTALL)
        self.assertIsNotNone(render_match, "render() not found in index.html")
        render_body = render_match.group(1)
        self.assertIn("refreshAtmStrikeFromLtp(liveLtp, positionOpen)", render_body)
        self.assertNotIn("suggestSymbol()", render_body)

        trade_match = re.search(
            r"async function executeTrade\(action\) \{(.*?)\n    \}",
            html,
            re.DOTALL,
        )
        self.assertIsNotNone(trade_match, "executeTrade() not found in index.html")
        self.assertNotIn("refreshAtmStrikeFromLtp", trade_match.group(1))


if __name__ == "__main__":
    unittest.main()
