import unittest
from datetime import datetime
from unittest.mock import patch

from common.config import get_next_expiry
from web.shared.symbol_helpers import suggest_option_symbol


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
        # Thu 24 Sep 2026 after 15:30 → Thu 1 Oct.
        self.assertEqual(self._expiry(datetime(2026, 9, 24, 15, 31), "SENSEX"), "261001")

    def test_nifty_monthly_expiry_uses_month_name(self):
        # Wed 23 Sep 2026 → Tue 29 Sep, last Tuesday of the month.
        self.assertEqual(self._expiry(datetime(2026, 9, 23, 14, 51), "NIFTY"), "26SEP")


if __name__ == "__main__":
    unittest.main()
