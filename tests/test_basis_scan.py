"""Inverse-contract research accounting and public-data-only boundaries."""
import math
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from btc_lab.basis_scan import (DAY_MS, funding_summary, inverse_execution_price,
                                inverse_pair_pnl, main, margin_capacity, public_get, scan, scenario)


class BasisScanTests(unittest.TestCase):
    def test_pair_btc_pnl_cancels_common_terminal_price(self):
        expected = 100 * (1 / 80000 - 1 / 82000)
        for terminal in (20000, 80000, 160000):
            self.assertAlmostEqual(inverse_pair_pnl(100, 80000, 82000, terminal, terminal), expected)

    def test_exit_basis_does_not_cancel(self):
        common = inverse_pair_pnl(100, 80000, 82000, 90000, 90000)
        adverse = inverse_pair_pnl(100, 80000, 82000, 89000, 90000)
        self.assertLess(adverse, common)

    def test_inverse_book_price_harmonic_not_arithmetic(self):
        actual = inverse_execution_price([[100, 1], [200, 1]], 2)
        self.assertAlmostEqual(actual, 400 / 3)
        self.assertAlmostEqual(2 / actual, 1 / 100 + 1 / 200)

    def test_insufficient_depth_and_invalid_prices_reject(self):
        for book, quantity in (([[100, 1]], 2), ([[0, 1]], 1), ([[100, 1]], 0), ([[math.nan, 1]], 1)):
            with self.assertRaises(ValueError):
                inverse_execution_price(book, quantity)

    def test_funding_sign_and_four_fees(self):
        long = scenario(100, 80000, 80000, 80000, 1, 10, .0003, .0005)
        short = scenario(100, 80000, 80000, 80000, -1, 10, .0003, .0005)
        self.assertAlmostEqual(long["funding_btc"], -100 / 80000 * .003)
        self.assertAlmostEqual(short["funding_btc"], -long["funding_btc"])
        self.assertAlmostEqual(long["fee_btc"], 4 * 100 / 80000 * .0005)
        self.assertLess(long["net_btc"], 0)

    def test_funding_uses_event_marks_and_inclusive_window(self):
        now = 10 * DAY_MS
        rows = [{"fundingTime": now-DAY_MS, "fundingRate": ".001", "markPrice": "50000"},
                {"fundingTime": now, "fundingRate": "-.0005", "markPrice": "100000"},
                {"fundingTime": now+1, "fundingRate": "99", "markPrice": "1"}]
        result = funding_summary(rows, now, 1)
        self.assertEqual(result["count"], 2)
        self.assertAlmostEqual(result["rate_per_day"], .0005)
        self.assertAlmostEqual(result["long_paid_btc_per_100usd_with_known_marks"], .0000015)

    def test_capacity_reserves_cash_counts_both_legs_and_entry_fees(self):
        capacity = margin_capacity(80000, 80000, 100, .007, 3, .25, .0005)
        self.assertEqual(capacity, 6)
        cost = 100 * 2 / 80000 * (1 / 3 + .0005)
        self.assertLessEqual(capacity * cost, .007 * .75)
        self.assertGreater((capacity + 1) * cost, .007 * .75)

    def test_reject_non_public_api_before_network(self):
        for path in ("/dapi/v1/order", "/dapi/v1/account", "https://example.org"):
            with self.assertRaises(ValueError):
                public_get(path)

    def test_unknown_future_funding_can_reverse_apparent_profit(self):
        zero = scenario(100, 80000, 81000, 80000, 1, 90, 0, .0005)
        paid = scenario(100, 80000, 81000, 80000, 1, 90, .0003, .0005)
        self.assertGreater(zero["net_btc"], 0)
        self.assertLess(paid["net_btc"], 0)

    def test_existing_artifact_rejects_before_network_and_preserves_content(self):
        for name in ("public_snapshot.json", "basis_report.json"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                output = Path(temp_dir)
                artifact = output / name
                artifact.write_text("original research", encoding="utf-8")
                with patch("btc_lab.basis_scan.public_get") as network:
                    with self.assertRaises(FileExistsError):
                        scan(output)
                    network.assert_not_called()
                self.assertEqual(artifact.read_text(encoding="utf-8"), "original research")

    def test_cli_default_output_contains_utc_timestamp_and_microseconds(self):
        fixed_time = datetime(2026, 9, 24, 12, 34, 56, 123456, tzinfo=timezone.utc)
        empty_report = {"generated_utc": "test", "decision": "test", "perp_delivery_pairs": []}
        with patch("btc_lab.basis_scan.datetime") as date_type, \
                patch("btc_lab.basis_scan.scan", return_value=empty_report) as run_scan, \
                patch("sys.argv", ["basis_scan"]), patch("builtins.print"):
            date_type.now.return_value = fixed_time
            main()
            date_type.now.assert_called_once_with(timezone.utc)
            self.assertEqual(run_scan.call_args.args[0], Path("btc_lab/state/basis_20260924T123456_123456Z"))


if __name__ == "__main__":
    unittest.main()
