import unittest
from datetime import date, datetime
from unittest.mock import patch

from common import scrip_master
from common.nse_calendar import is_trading_day, previous_trading_day
from common.nse_expiry import (
    effective_expiry_date,
    expiry_date_to_token,
    nominal_monthly_expiry,
    nominal_weekly_tuesday,
    next_nifty_expiry_date,
)
from common.time_utils import MARKET_TZ


class NseCalendarTests(unittest.TestCase):
    def test_weekend_not_trading(self):
        self.assertFalse(is_trading_day(date(2026, 3, 7)))  # Saturday

    def test_holi_2026_is_holiday(self):
        self.assertFalse(is_trading_day(date(2026, 3, 3)))

    def test_previous_trading_day_from_holiday_tuesday(self):
        # Holi Tue 2026-03-03 -> Mon 2026-03-02
        self.assertEqual(previous_trading_day(date(2026, 3, 3)), date(2026, 3, 2))


class NiftyExpiryHolidayTests(unittest.TestCase):
    def test_effective_expiry_prepone_holiday_tuesday(self):
        nominal = date(2026, 3, 3)  # Holi Tuesday
        self.assertEqual(effective_expiry_date(nominal), date(2026, 3, 2))

    def test_nominal_weekly_tuesday(self):
        self.assertEqual(nominal_weekly_tuesday(date(2026, 3, 1)), date(2026, 3, 3))

    def test_nominal_monthly_last_tuesday(self):
        self.assertEqual(nominal_monthly_expiry(2026, 3), date(2026, 3, 31))

    def test_holiday_adjusted_token_for_holi_week(self):
        eff = effective_expiry_date(date(2026, 3, 3))
        self.assertEqual(expiry_date_to_token(eff), "26302")

    @patch("common.nse_expiry._as_ist")
    def test_next_expiry_before_close_on_expiry_day(self, mock_ist):
        mock_ist.return_value = datetime(2026, 3, 2, 10, 0, tzinfo=MARKET_TZ)
        self.assertEqual(next_nifty_expiry_date(), date(2026, 3, 2))

    @patch("common.nse_expiry._as_ist")
    def test_next_expiry_after_close_rolls_forward(self, mock_ist):
        mock_ist.return_value = datetime(2026, 3, 2, 16, 0, tzinfo=MARKET_TZ)
        nxt = next_nifty_expiry_date()
        self.assertGreater(nxt, date(2026, 3, 2))


class ScripMasterContractTests(unittest.TestCase):
    def tearDown(self):
        scrip_master._token_cache = {}
        scrip_master._scrip_master_df = None

    def test_resolve_does_not_jump_to_far_month(self):
        scrip_master._token_cache = {
            "NIFTY26MAR25000CE": "111",
            "NIFTY26DEC25000CE": "999",
        }
        resolved = scrip_master.resolve_trading_symbol("NIFTY2621025000CE")
        self.assertIsNone(resolved)

    def test_validate_option_contract_exact_match(self):
        scrip_master._token_cache = {"NIFTY26MAR25000CE": "42"}
        result = scrip_master.validate_option_contract("NIFTY", "26MAR", 25000, "CE")
        self.assertTrue(result["ok"])
        self.assertEqual(result["trading_symbol"], "NIFTY26MAR25000CE")
        self.assertEqual(result["instrument_token"], "42")

    def test_validate_rejects_missing_contract(self):
        scrip_master._token_cache = {}
        result = scrip_master.validate_option_contract("NIFTY", "26MAR", 25000, "CE")
        self.assertFalse(result["ok"])

    def test_list_valid_nifty_expiries_prefers_near_weekly(self):
        scrip_master._token_cache = {
            "NIFTY2631725000CE": "1",
            "NIFTY26DEC25000CE": "2",
        }
        with patch("common.nse_expiry.theoretical_expiry_tokens", return_value=["26317", "26DEC"]):
            expiries = scrip_master.list_valid_nifty_expiries(date(2026, 3, 1))
        self.assertEqual(expiries[0], "26317")
        self.assertNotEqual(scrip_master.get_nearest_valid_nifty_expiry(date(2026, 3, 1)), "26DEC")

    def test_legacy_october_encoding_resolves(self):
        scrip_master._token_cache = {"NIFTY26O2125000CE": "55"}
        resolved = scrip_master.resolve_trading_symbol("NIFTY26102125000CE")
        self.assertEqual(resolved, "NIFTY26O2125000CE")

    def test_expiry_sort_key_monthly_october_not_fallback(self):
        monthly = scrip_master._expiry_sort_key("26OCT")
        self.assertEqual(monthly, (2026, 10, 28))
        self.assertNotEqual(monthly, (9999, 12, 31))

    def test_expiry_sort_key_october_weekly_26O19(self):
        weekly = scrip_master._expiry_sort_key("26O19")
        self.assertEqual(weekly, (2026, 10, 19))
        self.assertNotEqual(weekly, (9999, 12, 31))

    def test_list_valid_nifty_expiries_oct_2026_prefers_26O19_over_26OCT(self):
        scrip_master._token_cache = {
            "NIFTY26O1925000CE": "1",
            "NIFTY26OCT25000CE": "2",
        }
        with patch(
            "common.nse_expiry.theoretical_expiry_tokens",
            return_value=["26O19", "26OCT"],
        ):
            expiries = scrip_master.list_valid_nifty_expiries(date(2026, 10, 16))
        self.assertEqual(expiries[0], "26O19")
        self.assertEqual(
            scrip_master.get_nearest_valid_nifty_expiry(date(2026, 10, 16)),
            "26O19",
        )


if __name__ == "__main__":
    unittest.main()
