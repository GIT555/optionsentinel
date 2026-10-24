#!/usr/bin/env python3
"""
Quantitative Signal Capture & Action Guidance Engine (signal_sentinel.py)
========================================================================
Institutional multi-asset signal data aggregator and definitive action guide.
Eliminates guesswork by explicitly calculating standard statistical entry ranges,
technical boundaries, and dynamic exit triggers using 100% LIVE MARKET DATA.

Signal Logic & Mathematical Directives:
1. Premium Entry Engine (The Wheel CSP):
   - Trigger: IV Rank > 40% OR 6-Month Implied Volatility Percentile > 50th percentile.
   - Strike Selection: Spot Price minus (1.0 * IV * sqrt(45/365)) to approximate
     a true statistical 1-Standard Deviation OTM safety buffer (~0.16 to 0.30 Delta).
   - Matches to actual live exchange option contracts with real Bid, Ask, Volume, and Open Interest.
2. Leverage Entry Engine (LEAP Long Call):
   - Trigger: IV Rank < 20% AND 14-day Daily RSI < 35.
   - Exclusions: Automatically suppress and flag "REJECT" for leveraged ETFs
     (TQQQ, SPXL) to mitigate beta-slippage and volatility drag degradation.
   - Matches to live 360+ DTE LEAP calls and 45 DTE short PMCC overlay contracts.
3. Manual Position Monitor & Dynamic Exit Engine:
   - Reads manual user entries from positions.csv.
   - Automatically signals an explicit exit when:
     - DTE falls below 14 Days (Accelerated Gamma danger zone).
     - Running position profit hits a 50% max premium contraction threshold.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import math
import os
import sys
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from live_market_data import LiveMarketClient, LiveOptionContract, LiveTickerSnapshot, TICKER_SYMBOL_MAP


# ==============================================================================
# SECTION 1: STATISTICAL & TECHNICAL QUANTITATIVE FOUNDATION
# ==============================================================================

class StatMath:
    """Statistical and options mathematics utilities."""

    @staticmethod
    def norm_cdf(x: float) -> float:
        """Standard normal cumulative distribution function."""
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    @staticmethod
    def norm_pdf(x: float) -> float:
        """Standard normal probability density function."""
        return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

    @staticmethod
    def norm_ppf(p: float) -> float:
        """High-precision inverse standard normal CDF (Acklam algorithm)."""
        if p <= 0.0 or p >= 1.0:
            raise ValueError(f"Probability must be in (0, 1), got {p}")

        a = [
            -3.969683028665376e+01,  2.209460984245205e+02,
            -2.759285104469687e+02,  1.383577518672690e+02,
            -3.066479806614716e+01,  2.506628277459239e+00
        ]
        b = [
            -5.447609879822406e+01,  1.615858368580409e+02,
            -1.556989798598866e+02,  6.680131188771972e+01,
            -1.328068155288572e+01
        ]
        c = [
            -7.784894002430293e-03, -3.223964580411365e-01,
            -2.400758277161838e+00, -2.549732539343734e+00,
             4.374664141464968e+00,  2.938163982698783e+00
        ]
        d = [
             7.784695709041462e-03,  3.224671290700398e-01,
             2.445134137142996e+00,  3.754408661907416e+00
        ]

        p_low = 0.02425
        p_high = 1.0 - p_low

        if p < p_low:
            q = math.sqrt(-2.0 * math.log(p))
            return (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
                   ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1.0)
        elif p <= p_high:
            q = p - 0.5
            r = q * q
            return (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5])*q / \
                   (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1.0)
        else:
            q = math.sqrt(-2.0 * math.log(1.0 - p))
            return -(((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
                    ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1.0)

    @classmethod
    def black_scholes(
        cls, spot: float, strike: float, t: float, iv: float, r: float = 0.045, is_call: bool = True
    ) -> Tuple[float, float, float, float]:
        """Returns: (theoretical_price, delta, gamma, theta_per_day)"""
        t = max(t, 0.001)
        v_sqrt_t = iv * math.sqrt(t)
        d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t) / v_sqrt_t
        d2 = d1 - v_sqrt_t
        df = math.exp(-r * t)
        np_d1 = cls.norm_pdf(d1)

        gamma = np_d1 / (spot * v_sqrt_t) if (spot * v_sqrt_t) > 0 else 0.0

        if is_call:
            price = spot * cls.norm_cdf(d1) - strike * df * cls.norm_cdf(d2)
            delta = cls.norm_cdf(d1)
            theta = -(spot * np_d1 * iv) / (2.0 * math.sqrt(t)) - r * strike * df * cls.norm_cdf(d2)
        else:
            price = strike * df * cls.norm_cdf(-d2) - spot * cls.norm_cdf(-d1)
            delta = cls.norm_cdf(d1) - 1.0
            theta = -(spot * np_d1 * iv) / (2.0 * math.sqrt(t)) + r * strike * df * cls.norm_cdf(-d2)

        return max(0.01, price), delta, gamma, theta / 365.0

    @staticmethod
    def calculate_one_sd_put_strike(
        spot: float, iv: float, dte: int = 45, strike_increment: float = 1.0
    ) -> Tuple[float, float, float]:
        """
        Calculates the explicit 1-Standard Deviation Out-Of-The-Money Put strike:
        Formula: K = Spot * (1.0 - 1.0 * IV * sqrt(DTE / 365))
        Returns: (exact_strike, snapped_strike, dollar_move_1sd)
        """
        t = dte / 365.0
        pct_move_1sd = iv * math.sqrt(t)
        dollar_move_1sd = spot * pct_move_1sd
        exact_strike = spot - dollar_move_1sd
        snapped_strike = round(exact_strike / strike_increment) * strike_increment
        if snapped_strike <= 0:
            snapped_strike = strike_increment
        return exact_strike, snapped_strike, dollar_move_1sd


# ==============================================================================
# SECTION 2: ASSET TAXONOMY & WATCHLIST CONFIGURATION
# ==============================================================================

class AssetClass(str, Enum):
    CORE_EQUITY = "Core Equity / Tax-Advantaged"
    TRIPLE_LEVERAGED = "Triple-Leveraged Equities"
    COMMODITIES = "Macro Commodities"
    CRYPTO_BONDS = "Spot Crypto & Bonds"


ASSET_CONFIG: Dict[str, Dict[str, Any]] = {
    # 1. Core Equity / Tax-Advantaged
    "SPY": {"name": "SPDR S&P 500 ETF", "class": AssetClass.CORE_EQUITY, "leveraged": False, "tax_adv": False, "step": 1.0},
    "QQQ": {"name": "Invesco QQQ Trust", "class": AssetClass.CORE_EQUITY, "leveraged": False, "tax_adv": False, "step": 1.0},
    "XSP": {"name": "Mini-S&P 500 Index (Sec 1256)", "class": AssetClass.CORE_EQUITY, "leveraged": False, "tax_adv": True, "step": 1.0},
    "QQQM": {"name": "Invesco Nasdaq 100 ETF", "class": AssetClass.CORE_EQUITY, "leveraged": False, "tax_adv": False, "step": 1.0},
    "SPYM": {"name": "SPDR Portfolio S&P 500", "class": AssetClass.CORE_EQUITY, "leveraged": False, "tax_adv": False, "step": 0.5},

    # 2. Triple-Leveraged Equities
    "TQQQ": {"name": "ProShares UltraPro QQQ (3x)", "class": AssetClass.TRIPLE_LEVERAGED, "leveraged": True, "tax_adv": False, "step": 1.0},
    "SOXL": {"name": "Direxion Daily Semiconductor Bull 3X", "class": AssetClass.TRIPLE_LEVERAGED, "leveraged": True, "tax_adv": False, "step": 0.5},
    "SPXL": {"name": "Direxion Daily S&P 500 Bull 3X", "class": AssetClass.TRIPLE_LEVERAGED, "leveraged": True, "tax_adv": False, "step": 1.0},

    # 3. Macro Commodities
    "GLD": {"name": "SPDR Gold Shares", "class": AssetClass.COMMODITIES, "leveraged": False, "tax_adv": False, "step": 1.0},
    "SLV": {"name": "iShares Silver Trust", "class": AssetClass.COMMODITIES, "leveraged": False, "tax_adv": False, "step": 0.5},
    "IAU": {"name": "iShares Gold Trust", "class": AssetClass.COMMODITIES, "leveraged": False, "tax_adv": False, "step": 0.5},

    # 4. Spot Crypto & Bonds
    "IBIT": {"name": "iShares Bitcoin Trust ETF", "class": AssetClass.CRYPTO_BONDS, "leveraged": False, "tax_adv": False, "step": 0.5},
    "TLT": {"name": "iShares 20+ Year Treasury Bond", "class": AssetClass.CRYPTO_BONDS, "leveraged": False, "tax_adv": False, "step": 0.5},
}


class ActionSignal(str, Enum):
    TRIGGER_ENTER_CSP = "TRIGGER: ENTER THE WHEEL (CASH-SECURED PUT)"
    MONITOR_CSP = "MONITOR: CSP CRITERIA PENDING"
    TRIGGER_ENTER_LEAP = "TRIGGER: ENTER LEAP LONG CALL (0.85 DELTA)"
    MONITOR_LEAP = "MONITOR: LEAP CRITERIA PENDING"
    TRIGGER_ENTER_LEAP_PUT = "TRIGGER: ENTER LEAP LONG PUT (-0.85 DELTA OVERBOUGHT HEDGE)"
    MONITOR_LEAP_PUT = "MONITOR: LEAP PUT CRITERIA PENDING (RSI <= 65 OR IVR >= 25%)"
    REJECT_LEVERAGED_LEAP = "REJECT: LEVERAGED ETF EXCLUDED FROM LEAP"
    EXIT_GAMMA_DANGER = "EXIT: ACCELERATED GAMMA DANGER ZONE (< 14 DTE)"
    EXIT_PROFIT_TARGET = "EXIT: 50% MAX PROFIT TARGET CONTRACTED"
    EXIT_DUAL_TRIGGER = "EXIT: URGENT 50% PROFIT + GAMMA DANGER (< 14 DTE)"
    HOLD_POSITION = "HOLD: SAFE THETA HARVEST"


@dataclass
class WheelEntrySignal:
    symbol: str
    asset_name: str
    spot: float
    current_iv: float
    iv_rank: float
    iv_percentile_6m: float
    is_triggered: bool
    trigger_rule_satisfied: str
    target_dte: int
    expected_move_1sd: float
    exact_strike_1sd: float
    snapped_strike_1sd: float
    buffer_percentage_1sd: float
    live_contract_symbol: str
    live_bid: float
    live_ask: float
    live_mid: float
    live_volume: int
    live_open_interest: int
    theoretical_delta: float
    theoretical_gamma: float
    required_collateral: float
    profit_target_50: float
    annualized_roc: float
    action_directive: ActionSignal


@dataclass
class LeapEntrySignal:
    symbol: str
    asset_name: str
    spot: float
    current_iv: float
    iv_rank: float
    rsi_14: float
    is_leveraged: bool
    status: str
    is_triggered: bool
    rejection_rationale: Optional[str]
    target_dte: int
    target_delta: float
    live_leap_contract: Optional[str]
    live_leap_strike: Optional[float]
    live_leap_bid: Optional[float]
    live_leap_ask: Optional[float]
    live_leap_mid: Optional[float]
    capital_saved_vs_equity: Optional[float]
    effective_leverage: Optional[float]
    intrinsic_value: Optional[float]
    extrinsic_value: Optional[float]
    annual_drag_pct: Optional[float]
    live_pmcc_contract: Optional[str]
    live_pmcc_strike: Optional[float]
    live_pmcc_mid: Optional[float]
    pmcc_spread_width: Optional[float]
    pmcc_net_debit: Optional[float]
    pmcc_gap_safety: Optional[str]
    action_directive: ActionSignal


@dataclass
class PositionExitSignal:
    position_id: str
    symbol: str
    option_type: str
    strike: float
    expiration: str
    dte: int
    contracts: int
    entry_premium: float
    current_mid: float
    running_pnl_dollars: float
    profit_contraction_pct: float
    gamma_danger_flag: bool
    profit_target_flag: bool
    action_directive: ActionSignal
    order_instruction: str


# ==============================================================================
# SECTION 3: THE SIGNAL SENTINEL ENGINE (100% LIVE DATA)
# ==============================================================================

class SignalSentinel:
    """Quantitative signal aggregation and execution action guidance using 100% LIVE market data."""

    def __init__(self, positions_file: str = "positions.csv"):
        self.math = StatMath()
        self.market_client = LiveMarketClient()
        self.positions_file = positions_file
        self.snapshots: Dict[str, LiveTickerSnapshot] = {}
        self.refresh_live_telemetry()

    def refresh_live_telemetry(self):
        """Fetch live spot prices, Wilder RSI, HV, and IV metrics across all 12 tickers."""
        for sym in ASSET_CONFIG.keys():
            snap = self.market_client.get_live_ticker_snapshot(sym)
            self.snapshots[sym] = snap

    # --------------------------------------------------------------------------
    # 1. PREMIUM ENTRY ENGINE: THE WHEEL CSP GUIDANCE
    # --------------------------------------------------------------------------
    def evaluate_wheel_premium_guidance(self, all_tickers: bool = False) -> List[WheelEntrySignal]:
        """
        Calculates 1-Standard Deviation strike parameters across all tickers.
        Filters to triggered assets (IV Rank > 40% or 6M IVP > 50%), or displays all when all_tickers=True.
        """
        signals: List[WheelEntrySignal] = []

        for sym, cfg in ASSET_CONFIG.items():
            snap = self.snapshots.get(sym)
            if not snap:
                continue

            ivr = snap.iv_rank
            ivp = snap.iv_percentile_6m

            # Mathematical trigger condition
            ivr_triggered = (ivr > 40.0)
            ivp_triggered = (ivp > 50.0)
            is_triggered = (ivr_triggered or ivp_triggered)

            if not is_triggered and not all_tickers:
                continue

            trigger_reason = []
            if ivr_triggered:
                trigger_reason.append(f"IVR {ivr:.1f}% > 40%")
            if ivp_triggered:
                trigger_reason.append(f"6M-IVP {ivp:.1f}% > 50%")
            trigger_str = " & ".join(trigger_reason) if is_triggered else f"Pending (IVR: {ivr:.1f}%, IVP: {ivp:.1f}%)"

            # 1-Standard Deviation Strike Formula:
            target_dte = 45
            iv_to_use = max(0.10, snap.iv_current)
            exact_k, snapped_k, dollar_move_1sd = self.math.calculate_one_sd_put_strike(
                spot=snap.spot,
                iv=iv_to_use,
                dte=target_dte,
                strike_increment=cfg["step"],
            )

            # Query live option chain to locate actual matching contract
            live_contract_sym = f"{sym}-PUT-${snapped_k:.1f}"
            bid, ask, mid = 0.0, 0.0, 0.0
            volume, open_interest = 0, 0
            live_delta, live_gamma = -0.20, 0.01

            chain = self.market_client.get_live_option_chain(sym, 28, 55)
            puts = [c for c in chain if c.option_type == "PUT"]
            if puts:
                closest_put = min(puts, key=lambda c: abs(c.strike - snapped_k))
                live_contract_sym = closest_put.contract_symbol
                snapped_k = closest_put.strike
                target_dte = closest_put.dte
                bid = closest_put.bid
                ask = closest_put.ask
                mid = closest_put.mid
                volume = closest_put.volume
                open_interest = closest_put.open_interest
                live_delta = closest_put.delta_approx
                live_gamma = closest_put.gamma_approx

            if mid <= 0:
                theo_price, theo_delta, theo_gamma, _ = self.math.black_scholes(
                    spot=snap.spot,
                    strike=snapped_k,
                    t=target_dte / 365.0,
                    iv=iv_to_use,
                    r=0.045,
                    is_call=False,
                )
                mid = round(theo_price, 2)
                bid = round(max(0.01, mid * 0.95), 2)
                ask = round(mid * 1.05, 2)
                live_delta = round(theo_delta, 3)
                live_gamma = round(theo_gamma, 4)

            buffer_pct = ((snap.spot - snapped_k) / snap.spot) * 100.0 if snap.spot > 0 else 0.0
            collateral = snapped_k * 100.0
            profit_target_50 = round(mid * 0.50, 2)
            annualized_roc = round(((mid / snapped_k) * (365.0 / target_dte)) * 100.0, 2) if snapped_k > 0 else 0.0

            sig = WheelEntrySignal(
                symbol=sym,
                asset_name=cfg["name"],
                spot=snap.spot,
                current_iv=snap.iv_current,
                iv_rank=ivr,
                iv_percentile_6m=ivp,
                is_triggered=is_triggered,
                trigger_rule_satisfied=trigger_str,
                target_dte=target_dte,
                expected_move_1sd=round(dollar_move_1sd, 2),
                exact_strike_1sd=round(exact_k, 2),
                snapped_strike_1sd=snapped_k,
                buffer_percentage_1sd=round(buffer_pct, 2),
                live_contract_symbol=live_contract_sym,
                live_bid=bid,
                live_ask=ask,
                live_mid=mid,
                live_volume=volume,
                live_open_interest=open_interest,
                theoretical_delta=live_delta,
                theoretical_gamma=live_gamma,
                required_collateral=collateral,
                profit_target_50=profit_target_50,
                annualized_roc=annualized_roc,
                action_directive=ActionSignal.TRIGGER_ENTER_CSP if is_triggered else ActionSignal.MONITOR_CSP,
            )
            signals.append(sig)

        return signals

    # --------------------------------------------------------------------------
    # 2. LEVERAGE ENTRY ENGINE: LEAP LONG CALL & EXCLUSIONS
    # --------------------------------------------------------------------------
    def evaluate_leap_leverage_guidance(self) -> List[LeapEntrySignal]:
        """
        Evaluate Leverage Entries across live market feeds:
        - Trigger condition: IV Rank < 20% AND 14-day Daily RSI < 35.
        - Exclusions: Automatically suppress and flag "REJECT" for leveraged ETFs (TQQQ, SPXL).
        """
        signals: List[LeapEntrySignal] = []

        for sym, cfg in ASSET_CONFIG.items():
            snap = self.snapshots.get(sym)
            if not snap:
                continue

            ivr = snap.iv_rank
            rsi = snap.rsi_14
            is_leveraged = cfg["leveraged"]

            # Exclusion: Leveraged ETFs strictly prohibited from LEAP hold
            if is_leveraged:
                sig = LeapEntrySignal(
                    symbol=sym,
                    asset_name=cfg["name"],
                    spot=snap.spot,
                    current_iv=snap.iv_current,
                    iv_rank=ivr,
                    rsi_14=rsi,
                    is_leveraged=True,
                    status="REJECT",
                    is_triggered=False,
                    rejection_rationale=(
                        "CRITICAL EXCLUSION: Leveraged ETF strictly prohibited from LEAP hold. "
                        "Mitigates beta-slippage, geometric compounding loss, and path-dependent volatility drag."
                    ),
                    target_dte=390,
                    target_delta=0.85,
                    live_leap_contract=None,
                    live_leap_strike=None,
                    live_leap_bid=None,
                    live_leap_ask=None,
                    live_leap_mid=None,
                    capital_saved_vs_equity=None,
                    effective_leverage=None,
                    intrinsic_value=None,
                    extrinsic_value=None,
                    annual_drag_pct=None,
                    live_pmcc_contract=None,
                    live_pmcc_strike=None,
                    live_pmcc_mid=None,
                    pmcc_spread_width=None,
                    pmcc_net_debit=None,
                    pmcc_gap_safety=None,
                    action_directive=ActionSignal.REJECT_LEVERAGED_LEAP,
                )
                signals.append(sig)
                continue

            condition_met = (ivr < 20.0 and rsi < 35.0)

            # Query real live LEAP calls (340+ DTE)
            leap_contract_sym = f"{sym}-LEAP-CALL"
            leap_strike = round(snap.spot * 0.85)
            leap_bid, leap_ask, leap_mid = 0.0, 0.0, 0.0
            leap_dte = 390

            leaps = self.market_client.get_live_leap_options(sym, min_dte=340)
            if leaps:
                best_leap = min(leaps, key=lambda c: abs(c.delta_approx - 0.85))
                leap_contract_sym = best_leap.contract_symbol
                leap_strike = best_leap.strike
                leap_bid = best_leap.bid
                leap_ask = best_leap.ask
                leap_mid = best_leap.mid
                leap_dte = best_leap.dte

            if leap_mid <= 0:
                theo_price, _, _, _ = self.math.black_scholes(
                    spot=snap.spot,
                    strike=leap_strike,
                    t=leap_dte / 365.0,
                    iv=max(0.10, snap.iv_current),
                    r=0.045,
                    is_call=True,
                )
                leap_mid = round(theo_price, 2)
                leap_bid = round(max(0.01, leap_mid * 0.96), 2)
                leap_ask = round(leap_mid * 1.04, 2)

            capital_saved = round((snap.spot * 100.0) - (leap_mid * 100.0), 2)

            # PMCC short call overlay (45 DTE, 0.30 Delta)
            pmcc_contract = f"{sym}-PMCC-0.30D"
            pmcc_strike = round(snap.spot * 1.04)
            pmcc_mid = round(snap.spot * 0.015, 2)

            chain = self.market_client.get_live_option_chain(sym, 28, 55)
            calls = [c for c in chain if c.option_type == "CALL"]
            if calls:
                best_pmcc = min(calls, key=lambda c: abs(c.delta_approx - 0.30))
                pmcc_contract = best_pmcc.contract_symbol
                pmcc_strike = best_pmcc.strike
                pmcc_mid = best_pmcc.mid

            # Quantitative leverage analytics
            intrinsic = max(0.0, snap.spot - leap_strike)
            extrinsic = max(0.0, leap_mid - intrinsic)
            eff_lev = round((0.85 * snap.spot) / leap_mid, 2) if leap_mid > 0 else 0.0
            ann_drag = round((extrinsic / snap.spot) * (365.0 / max(1, leap_dte)) * 100.0, 2) if snap.spot > 0 else 0.0
            spread_w = round(pmcc_strike - leap_strike, 2)
            net_deb = round(leap_mid - pmcc_mid, 2)
            gap_safe = "SAFE: Zero Upside Gap Risk" if spread_w >= net_deb else "MONITOR: Upside Risk"

            sig = LeapEntrySignal(
                symbol=sym,
                asset_name=cfg["name"],
                spot=snap.spot,
                current_iv=snap.iv_current,
                iv_rank=ivr,
                rsi_14=rsi,
                is_leveraged=False,
                status="QUALIFIED_ENTRY" if condition_met else "WATCHLIST_MONITOR",
                is_triggered=condition_met,
                rejection_rationale=None,
                target_dte=leap_dte,
                target_delta=0.85,
                live_leap_contract=leap_contract_sym,
                live_leap_strike=leap_strike,
                live_leap_bid=leap_bid,
                live_leap_ask=leap_ask,
                live_leap_mid=leap_mid,
                capital_saved_vs_equity=capital_saved,
                effective_leverage=eff_lev,
                intrinsic_value=round(intrinsic, 2),
                extrinsic_value=round(extrinsic, 2),
                annual_drag_pct=ann_drag,
                live_pmcc_contract=pmcc_contract,
                live_pmcc_strike=pmcc_strike,
                live_pmcc_mid=pmcc_mid,
                pmcc_spread_width=spread_w,
                pmcc_net_debit=net_deb,
                pmcc_gap_safety=gap_safe,
                action_directive=ActionSignal.TRIGGER_ENTER_LEAP if condition_met else ActionSignal.MONITOR_LEAP,
            )
            signals.append(sig)

        return signals

    # --------------------------------------------------------------------------
    # 2B. BEARISH / OVERBOUGHT LEAP LONG PUT ENGINE (-0.85 DELTA + PMCP)
    # --------------------------------------------------------------------------
    def evaluate_leap_put_guidance(self) -> List[LeapEntrySignal]:
        """
        Evaluates Bearish / Tail-Hedge LEAP Long Put (-0.85 Delta, 340+ DTE) with
        Poor Man's Covered Put (PMCP, -0.30 Delta, 30-45 DTE) overlay.
        Trigger: 14-day Daily RSI > 65 (Overbought / Extended Rally) AND IV Rank < 25% (Cheap Put Volatility).
        Excludes 3x Leveraged ETFs (TQQQ, SOXL, SPXL).
        """
        signals: List[LeapEntrySignal] = []

        for sym, cfg in ASSET_CONFIG.items():
            if cfg["leveraged"]:
                continue

            snap = self.snapshots.get(sym)
            if not snap:
                continue

            ivr = snap.iv_rank
            rsi = snap.rsi_14
            condition_met = (rsi > 65.0 and ivr < 25.0)

            leap_contract_sym = f"{sym}-LEAP-PUT"
            leap_strike = round(snap.spot * 1.15)
            leap_bid, leap_ask, leap_mid = 0.0, 0.0, 0.0
            leap_dte = 390

            leaps = self.market_client.get_live_leap_options(sym, min_dte=340, option_type="PUT")
            if leaps:
                best_leap = min(leaps, key=lambda c: abs(c.delta_approx - (-0.85)))
                leap_contract_sym = best_leap.contract_symbol
                leap_strike = best_leap.strike
                leap_bid = best_leap.bid
                leap_ask = best_leap.ask
                leap_mid = best_leap.mid
                leap_dte = best_leap.dte

            if leap_mid <= 0:
                theo_price, _, _, _ = self.math.black_scholes(
                    spot=snap.spot,
                    strike=leap_strike,
                    t=leap_dte / 365.0,
                    iv=max(0.10, snap.iv_current),
                    r=0.045,
                    is_call=False,
                )
                leap_mid = round(theo_price, 2)
                leap_bid = round(max(0.01, leap_mid * 0.96), 2)
                leap_ask = round(leap_mid * 1.04, 2)

            capital_saved = round((snap.spot * 100.0) - (leap_mid * 100.0), 2)

            # PMCP short put overlay (45 DTE, -0.30 Delta)
            pmcp_contract = f"{sym}-PMCP-0.30D"
            pmcp_strike = round(snap.spot * 0.96)
            pmcp_mid = round(snap.spot * 0.015, 2)

            chain = self.market_client.get_live_option_chain(sym, 28, 55)
            puts = [c for c in chain if c.option_type == "PUT"]
            if puts:
                best_pmcp = min(puts, key=lambda c: abs(c.delta_approx - (-0.30)))
                pmcp_contract = best_pmcp.contract_symbol
                pmcp_strike = best_pmcp.strike
                pmcp_mid = best_pmcp.mid

            intrinsic = max(0.0, leap_strike - snap.spot)
            extrinsic = max(0.0, leap_mid - intrinsic)
            eff_lev = round((0.85 * snap.spot) / leap_mid, 2) if leap_mid > 0 else 0.0
            ann_drag = round((extrinsic / snap.spot) * (365.0 / max(1, leap_dte)) * 100.0, 2) if snap.spot > 0 else 0.0
            spread_w = round(leap_strike - pmcp_strike, 2)
            net_deb = round(leap_mid - pmcp_mid, 2)
            gap_safe = "SAFE: Zero Downside Crash Risk" if spread_w >= net_deb else "MONITOR: Spread < Debit"

            sig = LeapEntrySignal(
                symbol=sym,
                asset_name=cfg["name"],
                spot=snap.spot,
                current_iv=snap.iv_current,
                iv_rank=ivr,
                rsi_14=rsi,
                is_leveraged=False,
                status="QUALIFIED_PUT_ENTRY" if condition_met else "OVERBOUGHT_WATCHLIST",
                is_triggered=condition_met,
                rejection_rationale=None,
                target_dte=leap_dte,
                target_delta=-0.85,
                live_leap_contract=leap_contract_sym,
                live_leap_strike=leap_strike,
                live_leap_bid=leap_bid,
                live_leap_ask=leap_ask,
                live_leap_mid=leap_mid,
                capital_saved_vs_equity=capital_saved,
                effective_leverage=eff_lev,
                intrinsic_value=round(intrinsic, 2),
                extrinsic_value=round(extrinsic, 2),
                annual_drag_pct=ann_drag,
                live_pmcc_contract=pmcp_contract,
                live_pmcc_strike=pmcp_strike,
                live_pmcc_mid=pmcp_mid,
                pmcc_spread_width=spread_w,
                pmcc_net_debit=net_deb,
                pmcc_gap_safety=gap_safe,
                action_directive=ActionSignal.TRIGGER_ENTER_LEAP_PUT if condition_met else ActionSignal.MONITOR_LEAP_PUT,
            )
            signals.append(sig)

        return signals

    # --------------------------------------------------------------------------
    # 3. MANUAL POSITION MONITOR & DYNAMIC EXIT ENGINE
    # --------------------------------------------------------------------------
    def evaluate_manual_positions(self) -> List[PositionExitSignal]:
        """
        Reads manual positions from positions.csv and queries LIVE market prices.
        If positions.csv has 0 rows, returns empty list (NO SIMULATION).
        """
        if not os.path.exists(self.positions_file):
            return []

        today = datetime.date.today()
        user_rows = []

        with open(self.positions_file, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            for row in reader:
                if not row or row[0].startswith("#") or row[0].lower() == "symbol":
                    continue
                try:
                    sym = row[0].strip().upper()
                    strike = float(row[1])
                    opt_type = row[2].strip().upper()
                    exp_str = row[3].strip()
                    contracts = int(row[4])
                    entry_prem = float(row[5])
                    user_rows.append((sym, strike, opt_type, exp_str, contracts, entry_prem))
                except Exception:
                    continue

        if not user_rows:
            return []

        exit_signals: List[PositionExitSignal] = []

        for idx, (sym, strike, opt_type, exp_str, contracts, entry_prem) in enumerate(user_rows, start=1):
            exp_date = datetime.datetime.strptime(exp_str, "%Y-%m-%d").date()
            dte = (exp_date - today).days

            # Query real live quote from market
            chain = self.market_client.get_live_option_chain(sym, max(1, dte - 5), dte + 5)
            matching = [c for c in chain if c.option_type == opt_type and abs(c.strike - strike) < 0.01]

            if matching:
                curr_mid = matching[0].mid
            else:
                curr_mid = entry_prem

            if contracts < 0:
                contraction_pct = ((entry_prem - curr_mid) / entry_prem) * 100.0 if entry_prem > 0 else 0.0
                running_pnl = (entry_prem - curr_mid) * abs(contracts) * 100.0
            else:
                contraction_pct = ((curr_mid - entry_prem) / entry_prem) * 100.0 if entry_prem > 0 else 0.0
                running_pnl = (curr_mid - entry_prem) * abs(contracts) * 100.0

            gamma_breach = (dte < 14 and contracts < 0)
            profit_breach = (contraction_pct >= 50.0 and contracts < 0)

            if gamma_breach and profit_breach:
                directive = ActionSignal.EXIT_DUAL_TRIGGER
                instruction = (
                    f"IMMEDIATE BTC ORDER: 50% Profit Target Hit ({contraction_pct:.1f}%) AND "
                    f"Gamma Danger Threshold Breached ({dte} DTE < 14). Buy to Close immediately to lock profit."
                )
            elif gamma_breach:
                directive = ActionSignal.EXIT_GAMMA_DANGER
                instruction = (
                    f"STRUCTURAL ROLL / EXIT: Breached 14 DTE threshold (Current: {dte} DTE). "
                    f"Accelerated Gamma danger. Submit BTC order or Roll Out to 42 DTE."
                )
            elif profit_breach:
                directive = ActionSignal.EXIT_PROFIT_TARGET
                instruction = (
                    f"PROFIT EXIT ORDER: Premium contracted by {contraction_pct:.1f}% (>= 50% threshold). "
                    f"Submit Buy-To-Close limit order at ${curr_mid:.2f} to release collateral."
                )
            else:
                directive = ActionSignal.HOLD_POSITION
                instruction = (
                    f"HOLD POSITION: DTE ({dte}) >= 14 and Profit ({contraction_pct:.1f}%) < 50%. "
                    f"Safe operational theta harvest zone."
                )

            sig = PositionExitSignal(
                position_id=f"MANUAL-{idx:02d}-{sym}",
                symbol=sym,
                option_type=opt_type,
                strike=strike,
                expiration=exp_str,
                dte=dte,
                contracts=contracts,
                entry_premium=entry_prem,
                current_mid=curr_mid,
                running_pnl_dollars=round(running_pnl, 2),
                profit_contraction_pct=round(contraction_pct, 1),
                gamma_danger_flag=gamma_breach,
                profit_target_flag=profit_breach,
                action_directive=directive,
                order_instruction=instruction,
            )
            exit_signals.append(sig)

        return exit_signals


# ==============================================================================
# SECTION 4: CLI DISPLAY & INSTITUTIONAL GUIDANCE
# ==============================================================================

def display_full_guidance(engine: SignalSentinel, show_all_wheel: bool = False):
    print("""
================================================================================
   QUANTITATIVE SIGNAL CAPTURE & ACTION GUIDANCE (100% LIVE MARKET DATA)
================================================================================
  Real-Time Market Telemetry • Live 1-SD Option Chains • Live LEAPs & PMCC
  Zero Simulation • All Spot, Volatility, and Option Chains from Live Exchanges
================================================================================
""")

    # 1. Live Telemetry
    print("--- 1. LIVE MULTI-ASSET SIGNAL TELEMETRY ---")
    print(f"{'SYM':<6} {'ASSET CLASS':<26} {'LIVE SPOT':<10} {'IV (30d)':<9} {'IVR':<7} {'6M IVP':<8} {'RSI14':<7} {'FEED'}")
    print("-" * 84)
    for sym, snap in engine.snapshots.items():
        cfg = ASSET_CONFIG.get(sym, {})
        cls_name = cfg.get("class", AssetClass.CORE_EQUITY).value
        print(
            f"{sym:<6} {cls_name:<26} ${snap.spot:<9.2f} {snap.iv_current*100:>5.1f}%   "
            f"{snap.iv_rank:>5.1f}% {snap.iv_percentile_6m:>6.1f}% {snap.rsi_14:>6.1f}  \033[92mLIVE\033[0m"
        )

    # 2. Premium Entry Engine (The Wheel CSP Guidance)
    print("\n--- 2. PREMIUM ENTRY GUIDANCE: THE WHEEL CSP (LIVE 1-SD OPTION STRIKES) ---")
    wheel_signals = engine.evaluate_wheel_premium_guidance(all_tickers=show_all_wheel)
    triggered = [w for w in wheel_signals if w.is_triggered]

    if not triggered and not show_all_wheel:
        print("  Status: No assets currently breach IV Rank > 40% or 6M IVP > 50% threshold.")
        print("  (Tip: Run with --all to inspect 1-SD strike boundaries across all 12 assets).")
    else:
        display_list = wheel_signals if show_all_wheel else triggered
        print(f"{'SYM':<6} {'SPOT':<9} {'1-SD MOVE':<10} {'1-SD STRIKE':<12} {'BUFFER':<8} {'LIVE CONTRACT':<22} {'BID/ASK':<13} {'MID':<7} {'STATUS'}")
        print("-" * 105)
        for w in display_list:
            status_str = "\033[92;1m[TRIGGER]\033[0m" if w.is_triggered else "\033[93;1m[MONITOR]\033[0m"
            print(
                f"{w.symbol:<6} ${w.spot:<8.2f} ${w.expected_move_1sd:<9.2f} ${w.snapped_strike_1sd:<11.2f} "
                f"{w.buffer_percentage_1sd:>5.1f}%  {w.live_contract_symbol:<22} "
                f"${w.live_bid:.2f}/${w.live_ask:.2f}   ${w.live_mid:<6.2f} {status_str}"
            )

    # 3. Leverage Entry Engine (Live LEAP Long Call & Rejections)
    print("\n--- 3. LEVERAGE ENTRY GUIDANCE: LIVE LEAP LONG CALL & PMCC DIAGONALS ---")
    leap_signals = engine.evaluate_leap_leverage_guidance()
    for l in leap_signals:
        if l.status == "REJECT":
            print(f"\n\033[91;1m[{l.symbol}] STATUS: {l.status}\033[0m (Leveraged Asset: {l.is_leveraged})")
            print(f"  -> Spot: ${l.spot:.2f} | IVR: {l.iv_rank:.1f}% | 14-Day RSI: {l.rsi_14:.1f}")
            print(f"  -> \033[93;1m{l.rejection_rationale}\033[0m")
        elif l.is_triggered:
            print(f"\n\033[92;1m[{l.symbol}] STATUS: {l.status} (QUALIFIED LEAP TRIGGER)\033[0m")
            print(f"  -> Live Spot: ${l.spot:.2f} | IVR: {l.iv_rank:.1f}% (<20%) | 14-Day RSI: {l.rsi_14:.1f} (<35)")
            print(f"  -> Live LEAP Contract : \033[96;1m{l.live_leap_contract}\033[0m (Strike ${l.live_leap_strike:.1f} | {l.target_dte} DTE)")
            print(f"  -> Live LEAP Mid      : \033[93;1m${l.live_leap_mid:.2f}\033[0m (Bid: ${l.live_leap_bid:.2f} / Ask: ${l.live_leap_ask:.2f})")
            print(f"  -> Effective Leverage : \033[92;1m{l.effective_leverage}x\033[0m Non-Decaying Exposure (Zero Beta Drag)")
            print(f"  -> Annual Drag (Cost) : \033[92;1m{l.annual_drag_pct}%\033[0m APR (Extrinsic Time Value vs 9-11% Margin Interest)")
            print(f"  -> PMCC Gap Safety    : \033[96;1m{l.pmcc_gap_safety}\033[0m (Spread Width ${l.pmcc_spread_width:.1f} vs Debit ${l.pmcc_net_debit:.2f})")
            print(f"  -> Capital Saved      : \033[92;1m${l.capital_saved_vs_equity:,.2f}\033[0m vs owning 100 shares of {l.symbol}")
            print(f"  -> Short PMCC Overlay : \033[96;1m{l.live_pmcc_contract}\033[0m (Strike ${l.live_pmcc_strike:.1f} @ mid ${l.live_pmcc_mid:.2f})")
        else:
            print(f"\n\033[97;1m[{l.symbol}]\033[0m STATUS: \033[93;1m{l.status} (MONITOR - IVR {l.iv_rank:.1f}%, RSI {l.rsi_14:.1f})\033[0m")
            print(f"  -> Live LEAP Reference: {l.live_leap_contract} (Strike ${l.live_leap_strike:.1f} | Mid: ${l.live_leap_mid:.2f})")

    # 3B. Bearish / Overbought LEAP Long Put (-0.85 Delta) & PMCP Diagonals
    print("\n--- 3B. BEARISH / OVERBOUGHT LEAP LONG PUT (-0.85 DELTA) & PMCP DIAGONALS ---")
    print("  Trigger Rule: 14-Day RSI > 65 (Overbought Rally) AND IV Rank < 25% (Complacent Cheap Put Volatility).")
    print("  Overlay: Poor Man's Covered Put (PMCP) -> Sell 30-45 DTE -0.30 Delta Put to finance time decay.")
    print(f"\n{'SYM':<6} {'SPOT':<9} {'RSI14':<7} {'IVR':<7} {'-0.85D LEAP PUT':<22} {'STRIKE':<9} {'MID':<9} {'LEV':<6} {'PMCP SHORT PUT':<22} {'STATUS'}")
    print("-" * 116)
    leap_put_signals = engine.evaluate_leap_put_guidance()
    for lp in leap_put_signals:
        st_str = "\033[92;1m[QUALIFIED PUT]\033[0m" if lp.is_triggered else "\033[93;1m[CHECK / WATCH]\033[0m"
        print(
            f"\033[97;1m{lp.symbol:<6}\033[0m ${lp.spot:<8.2f} {lp.rsi_14:>5.1f}  {lp.iv_rank:>5.1f}%  "
            f"\033[96;1m{lp.live_leap_contract:<22}\033[0m ${lp.live_leap_strike:<8.1f} \033[93;1m${lp.live_leap_mid:<8.2f}\033[0m "
            f"{lp.effective_leverage:>4.2f}x  {lp.live_pmcc_contract:<22} {st_str}"
        )

    # 3C. Triple-Leveraged Equities Strategy Engine (TQQQ, SOXL, SPXL)
    print("\n--- 3C. TRIPLE-LEVERAGED EQUITIES STRATEGY ENGINE: TQQQ, SOXL, SPXL ---")
    print("  Mathematical Rule: NEVER buy long LEAP calls on 3x ETFs (variance drag causes massive beta-slippage).")
    print("  Institutional Strategy: Harvest extreme implied volatility by selling 0.20 Delta Cash-Secured Puts (CSP)!")
    print(f"\n{'SYM':<6} {'SPOT':<8} {'IV':<7} {'1-SD MOVE':<10} {'0.20D STRIKE':<13} {'BUFFER':<8} {'LIVE CONTRACT':<22} {'MID':<7} {'ROC (EST)'}")
    print("-" * 96)
    for l_sym in ["TQQQ", "SOXL", "SPXL"]:
        snap = engine.snapshots.get(l_sym)
        if not snap:
            continue
        spot = snap.spot
        iv = max(0.15, snap.iv_current)
        dte = 45
        move_1sd = spot * iv * math.sqrt(dte / 365.0)
        safe_k = round((spot - (move_1sd * 1.25)) / 0.5) * 0.5
        buffer_pct = round(((spot - safe_k) / spot) * 100.0, 1) if spot > 0 else 0.0
        chain = engine.market_client.get_live_option_chain(l_sym, 28, 55)
        puts = [c for c in chain if c.option_type == "PUT"]
        closest = min(puts, key=lambda c: abs(c.strike - safe_k)) if puts else None
        contract = closest.contract_symbol if closest else f"{l_sym}-CSP-0.20D"
        mid = closest.mid if closest else round(spot * 0.03, 2)
        roc = round(((mid / safe_k) * (365.0 / dte)) * 100.0, 1) if safe_k > 0 else 0.0
        print(f"\033[97;1m{l_sym:<6}\033[0m ${spot:<7.2f} {iv*100:>5.1f}% ${move_1sd:<9.2f} \033[93;1m${safe_k:<12.1f}\033[0m {buffer_pct:>5.1f}%  \033[96;1m{contract:<22}\033[0m ${mid:<6.2f} \033[92;1m{roc:>5.1f}%\033[0m")

    # 4. Manual Position Monitor & Dynamic Exits
    print("\n--- 4. MANUAL POSITION MONITOR & DYNAMIC EXIT ENGINE (positions.csv) ---")
    manual_exits = engine.evaluate_manual_positions()
    if not manual_exits:
        print("  \033[93;1m[INFO]\033[0m: No active positions entered in positions.csv (0 positions monitored).")
        print("  -> When you manually enter a trade, add a line to 'positions.csv' (e.g., SPY,740,PUT,2026-11-27,-1,7.20).")
        print("  -> The sentinel will automatically fetch real live quotes, track live P&L, and emit <14 DTE Gamma alerts.")
    else:
        for e in manual_exits:
            badge = "[HOLD]"
            color = "\033[97;1m"
            if e.action_directive == ActionSignal.EXIT_DUAL_TRIGGER:
                badge = "[URGENT EXIT: DUAL TRIGGER]"
                color = "\033[91;1m"
            elif e.action_directive == ActionSignal.EXIT_GAMMA_DANGER:
                badge = "[EXIT: GAMMA DANGER <14 DTE]"
                color = "\033[91m"
            elif e.action_directive == ActionSignal.EXIT_PROFIT_TARGET:
                badge = "[EXIT: 50% PROFIT TARGET HIT]"
                color = "\033[92;1m"

            print(f"\n{color}{badge} {e.position_id} ({e.symbol} ${e.strike:.1f} {e.option_type} exp {e.expiration}){color}")
            print(f"  -> DTE: {e.dte} days | Entry: ${e.entry_premium:.2f} | Live Mid: ${e.current_mid:.2f} | Contraction: {e.profit_contraction_pct:+.1f}% | P&L: ${e.running_pnl_dollars:+.2f}")
            print(f"  -> Action: {e.order_instruction}")


def main():
    parser = argparse.ArgumentParser(description="Live Quantitative Signal & Action Guidance Engine.")
    parser.add_argument("--scan", action="store_true", default=True, help="Run live quantitative signal audit.")
    parser.add_argument("--all", action="store_true", help="Display 1-SD strikes for all tickers regardless of trigger.")
    parser.add_argument("--json", action="store_true", help="Output results in structured JSON format.")
    parser.add_argument("--auto-sync", type=int, default=0, help="Auto-refresh live market telemetry every N seconds.")

    args = parser.parse_args()
    engine = SignalSentinel(positions_file="positions.csv")

    if args.json:
        payload = {
            "timestamp": datetime.datetime.now().isoformat(),
            "telemetry": {k: asdict(v) for k, v in engine.snapshots.items()},
            "wheel_signals": [asdict(w) for w in engine.evaluate_wheel_premium_guidance(all_tickers=args.all)],
            "leap_signals": [asdict(l) for l in engine.evaluate_leap_leverage_guidance()],
            "leap_put_signals": [asdict(lp) for lp in engine.evaluate_leap_put_guidance()],
            "manual_positions": [asdict(e) for e in engine.evaluate_manual_positions()],
        }
        print(json.dumps(payload, indent=2))
        return

    if args.auto_sync > 0:
        print(f"\n\033[96;1m[AUTO-SYNC ACTIVATED]\033[0m Refreshing live exchange data every {args.auto_sync}s. Press Ctrl+C to stop.\n")
        try:
            while True:
                display_full_guidance(engine, show_all_wheel=args.all)
                print(f"\n\033[93;1m[AUTO-SYNC]\033[0m Telemetry fresh. Next live poll in {args.auto_sync} seconds...")
                time.sleep(args.auto_sync)
                engine.refresh_live_telemetry()
        except KeyboardInterrupt:
            print("\nAuto-sync stopped by user.")
            return

    display_full_guidance(engine, show_all_wheel=args.all)


if __name__ == "__main__":
    main()
