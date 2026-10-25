"""
Automated Comprehensive Test Suite for Option Sentinel & Signal Sentinel (100% Real Live Engine)
"""

import math
import unittest
from option_sentinel import (
    QuantMath,
    WATCHLIST_MATRIX,
)
from signal_sentinel import (
    StatMath,
    ActionSignal,
    ASSET_CONFIG,
)
from live_market_data import LiveMarketClient


class TestQuantitativeMath(unittest.TestCase):
    def test_normal_distribution_symmetry(self):
        self.assertAlmostEqual(QuantMath.norm_cdf(0.0), 0.5, places=6)
        self.assertAlmostEqual(QuantMath.norm_cdf(1.96), 0.975, places=2)
        self.assertAlmostEqual(QuantMath.norm_cdf(-1.96), 0.025, places=2)

    def test_norm_ppf_inversion(self):
        test_probabilities = [0.05, 0.16, 0.30, 0.50, 0.70, 0.84, 0.95]
        for p in test_probabilities:
            z = QuantMath.norm_ppf(p)
            recovered_p = QuantMath.norm_cdf(z)
            self.assertAlmostEqual(p, recovered_p, places=4)

    def test_one_sd_put_strike_formula(self):
        spot = 600.0
        iv = 0.20
        dte = 45
        exact_k, snapped_k, dollar_move = StatMath.calculate_one_sd_put_strike(spot, iv, dte, 1.0)
        expected_move = spot * iv * math.sqrt(45.0 / 365.0)
        self.assertAlmostEqual(dollar_move, expected_move, places=2)
        self.assertAlmostEqual(exact_k, spot - expected_move, places=2)
        self.assertTrue(snapped_k < spot)

    def test_black_scholes_pricing(self):
        spot = 500.0
        strike = 480.0
        iv = 0.25
        dte = 45
        price = QuantMath.black_scholes_price(spot, strike, dte, iv, is_call=False)
        self.assertTrue(price > 0.0)
        self.assertTrue(price < strike)

    def test_wilder_rsi_calculation(self):
        increasing = [100.0 + i for i in range(30)]
        self.assertEqual(LiveMarketClient._calculate_rsi(increasing, 14), 100.0)

        decreasing = [100.0 - i for i in range(30)]
        self.assertEqual(LiveMarketClient._calculate_rsi(decreasing, 14), 0.0)


class TestUniverseConfiguration(unittest.TestCase):
    def test_watchlist_coverage(self):
        symbols = set(WATCHLIST_MATRIX.keys())
        expected = {"SPY", "QQQ", "XSP", "QQQM", "SPYM", "TQQQ", "SOXL", "SPXL", "GLD", "SLV", "IAU", "IBIT", "TLT"}
        self.assertEqual(symbols, expected)

    def test_leveraged_flag_enforcement(self):
        self.assertTrue(WATCHLIST_MATRIX["TQQQ"].is_leveraged)
        self.assertTrue(WATCHLIST_MATRIX["SOXL"].is_leveraged)
        self.assertTrue(WATCHLIST_MATRIX["SPXL"].is_leveraged)
        self.assertFalse(WATCHLIST_MATRIX["SPY"].is_leveraged)
        self.assertFalse(WATCHLIST_MATRIX["GLD"].is_leveraged)

    def test_asset_config_alignment(self):
        self.assertEqual(set(ASSET_CONFIG.keys()), set(WATCHLIST_MATRIX.keys()))

    def test_fidelity_converter_equity_and_options(self):
        from convert_fidelity_positions import convert_latest_fidelity_to_positions
        src_csv, positions = convert_latest_fidelity_to_positions()
        self.assertTrue(len(positions) > 0)
        types = {p.option_type for p in positions}
        self.assertIn("EQUITY", types)
        self.assertTrue("PUT" in types or "CALL" in types)


if __name__ == "__main__":
    unittest.main()
