"""
Automated Comprehensive Test Suite for Option Sentinel & Signal Sentinel (100% Real Live Engine)
"""

import math
import unittest
from option_trading.server.app import (
    QuantMath,
    WATCHLIST_MATRIX,
)
from option_trading.core.signal_sentinel import (
    StatMath,
    ActionSignal,
    ASSET_CONFIG,
)
from option_trading.market.client import LiveMarketClient


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
        for non_lev in ["SPY", "QQQ", "XSP", "QQQM", "SPYM", "GLD", "SLV", "IAU", "IBIT", "TLT"]:
            self.assertFalse(WATCHLIST_MATRIX[non_lev].is_leveraged, f"{non_lev} should be eligible for LEAP/PMCC")

    def test_asset_config_alignment(self):
        self.assertEqual(set(ASSET_CONFIG.keys()), set(WATCHLIST_MATRIX.keys()))

    def test_fidelity_converter_equity_and_options(self):
        from option_trading.portfolio.fidelity import convert_latest_fidelity_to_positions
        src_csv, positions = convert_latest_fidelity_to_positions()
        self.assertTrue(len(positions) > 0)
        types = {p.option_type for p in positions}
        self.assertIn("EQUITY", types)
        self.assertTrue("PUT" in types or "CALL" in types)

    def test_schwab_readonly_client_safety(self):
        client = LiveMarketClient()
        status = client.schwab_client.get_status()
        self.assertEqual(status["mode"], "100% READ-ONLY (GET Market Data Only)")
        self.assertIn("connected", status)
        self.assertIn("days_remaining", status)

    def test_lookup_position_contract_matches_exact_expiration(self):
        from option_trading.market.client import LiveOptionContract
        client = LiveMarketClient()
        dummy_contract_21 = LiveOptionContract(
            contract_symbol="IAU261127C00079000", strike=79.0, dte=21, expiration="2026-11-27",
            option_type="CALL", bid=0.55, ask=0.85, mid=0.70, last_price=0.90, volume=2,
            open_interest=75, implied_volatility=0.25, delta_approx=0.26, gamma_approx=0.07,
            theta_approx=-0.03, in_the_money=False
        )
        dummy_contract_42 = LiveOptionContract(
            contract_symbol="IAU261218C00079000", strike=79.0, dte=42, expiration="2026-12-18",
            option_type="CALL", bid=1.10, ask=1.60, mid=1.35, last_price=1.25, volume=5,
            open_interest=100, implied_volatility=0.27, delta_approx=0.35, gamma_approx=0.05,
            theta_approx=-0.03, in_the_money=False
        )
        client._cache_chains["IAU_17_25"] = (1e12, [dummy_contract_21])
        client._cache_chains["IAU_28_55"] = (1e12, [dummy_contract_42])

        matched_21 = client.lookup_position_contract("IAU", "CALL", 79.0, 21, expiration="2026-11-27")
        self.assertIsNotNone(matched_21)
        self.assertEqual(matched_21.expiration, "2026-11-27")
        self.assertEqual(matched_21.mid, 0.70)

        matched_42 = client.lookup_position_contract("IAU", "CALL", 79.0, 42, expiration="2026-12-18")
        self.assertIsNotNone(matched_42)
        self.assertEqual(matched_42.expiration, "2026-12-18")
        self.assertEqual(matched_42.mid, 1.35)


if __name__ == "__main__":
    unittest.main()
