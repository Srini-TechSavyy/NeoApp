import unittest
from datetime import datetime
from unittest.mock import patch

from common.config import get_next_expiry, _format_neo_expiry
from common import scrip_master
from web.shared.symbol_helpers import parse_symbol_parts, suggest_option_symbol


class ExpirySymbolTests(unittest.TestCase):
    def _expiry(self, when: datetime, symbol: str) -> str:
        with patch("common.config.datetime") as mock_dt:
            mock_dt.now.return_value = when
            return get_next_expiry(symbol)

    def test_sensex_monthly_expiry_uses_month_name(self):
        # Wed 23 Sep 2026 → Thu 24 Sep, last Thursday of the month.
        expiry = self._expiry(datetime(2026, 9, 23, 14, 51), "SENSEX")
        self.assertEqual(expiry, "26SEP")
        with patch("web.shared.symbol_helpers.get_next_expiry", return_value=expiry):
            symbol = suggest_option_symbol("SENSEX", 74820, "CE", 0)
        self.assertEqual(symbol, "SENSEX26SEP74800CE")

    def test_sensex_weekly_expiry_keeps_yymdd(self):
        # Wed 16 Sep 2026 → Thu 17 Sep, not the monthly expiry.
        self.assertEqual(self._expiry(datetime(2026, 9, 16, 10, 0), "SENSEX"), "26917")

    def test_sensex_after_monthly_close_rolls_to_next_weekly(self):
        # Thu 24 Sep 2026 after 15:30 → Thu 1 Oct (letter-month weekly).
        self.assertEqual(self._expiry(datetime(2026, 9, 24, 15, 31), "SENSEX"), "26O01")

    def test_sensex_october_weekly_uses_letter_o(self):
        self.assertEqual(_format_neo_expiry(datetime(2026, 10, 1)), "26O01")

    def test_sensex_november_weekly_uses_letter_n(self):
        self.assertEqual(_format_neo_expiry(datetime(2026, 11, 5)), "26N05")

    def test_sensex_december_weekly_uses_letter_d(self):
        self.assertEqual(_format_neo_expiry(datetime(2026, 12, 3)), "26D03")

    def test_nifty_monthly_expiry_uses_month_name(self):
        # Wed 23 Sep 2026 → Tue 29 Sep, last Tuesday of the month.
        self.assertEqual(self._expiry(datetime(2026, 9, 23, 14, 51), "NIFTY"), "26SEP")

    def test_parse_letter_month_weekly_symbol(self):
        parts = parse_symbol_parts("SENSEX26O0172900CE")
        self.assertEqual(parts, ("SENSEX", "26O01", 72900, "CE"))

    def test_parse_legacy_numeric_october_weekly(self):
        # Legacy YYMMDD construction still parses so the resolver can normalize it.
        parts = parse_symbol_parts("SENSEX26100172900CE")
        self.assertEqual(parts, ("SENSEX", "261001", 72900, "CE"))


class ScripMasterResolveTests(unittest.TestCase):
    def tearDown(self):
        scrip_master._token_cache = {}
        scrip_master._scrip_master_df = None

    def test_resolve_maps_legacy_october_code_to_broker_symbol(self):
        scrip_master._token_cache = {
            "SENSEX26O0172900CE": "12345",
            "SENSEX26SEP72900CE": "99999",
        }
        resolved = scrip_master.resolve_trading_symbol("SENSEX26100172900CE")
        self.assertEqual(resolved, "SENSEX26O0172900CE")
        self.assertEqual(
            scrip_master.find_token_for_trading_symbol("SENSEX26100172900CE"),
            "12345",
        )

    def test_resolve_returns_exact_match(self):
        scrip_master._token_cache = {"SENSEX26O0172900CE": "12345"}
        self.assertEqual(
            scrip_master.resolve_trading_symbol("SENSEX26O0172900CE"),
            "SENSEX26O0172900CE",
        )


if __name__ == "__main__":
    unittest.main()
