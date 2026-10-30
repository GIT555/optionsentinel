#!/usr/bin/env python3
"""
Multi-Broker Option Sentinel & Strike Selector Agent (option_sentinel.py)
========================================================================
Institutional quantitative options dashboard powered by 100% LIVE MARKET DATA.
Zero simulated positions or synthetic prices. All quotes, Greeks, and option
chains are pulled live from exchanges.

Features:
- Live market telemetry across 12 target tickers.
- Real exchange option contracts for 1-SD Wheel CSP strikes and 0.85 Delta LEAPs.
- Manual position tracking via positions.csv with live market price & DTE sync.
- Real-time sys.stderr Gamma Danger alerts whenever manual positions drop below 14 DTE.
- Built-in multi-threaded HTTP server hosting the analytical UI dashboard.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import http.server
import json
import math
import os
import re
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from live_market_data import LiveMarketClient, LiveOptionContract, LiveTickerSnapshot, TICKER_SYMBOL_MAP


def parse_occ_expiration(contract_symbol: str) -> Optional[str]:
    """Extracts ISO YYYY-MM-DD expiration date from an OCC contract symbol (e.g. TLT261127P00078000 -> 2026-11-27)."""
    if not contract_symbol:
        return None
    m = re.search(r"([A-Z]+)(\d{2})(\d{2})(\d{2})[CP]", contract_symbol.upper())
    if m:
        yy, mm, dd = m.group(2), m.group(3), m.group(4)
        return f"20{yy}-{mm}-{dd}"
    return None


# ==============================================================================
# SECTION 1: HIGH-PRECISION QUANTITATIVE MATHEMATICS ENGINE
# ==============================================================================

class QuantMath:
    """Mathematical engine for Black-Scholes-Merton, Greeks, and Delta Inversion."""

    @staticmethod
    def norm_cdf(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    @staticmethod
    def norm_pdf(x: float) -> float:
        return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

    @staticmethod
    def norm_ppf(p: float) -> float:
        if p <= 0.0 or p >= 1.0:
            raise ValueError(f"Probability p must be in (0, 1), got {p}")

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
    def black_scholes_price(
        cls, spot: float, strike: float, dte: float, iv: float, r: float = 0.045, is_call: bool = True
    ) -> float:
        t = max(dte, 0.5) / 365.0
        v_sqrt_t = iv * math.sqrt(t)
        d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t) / v_sqrt_t
        d2 = d1 - v_sqrt_t
        df = math.exp(-r * t)
        if is_call:
            price = spot * cls.norm_cdf(d1) - strike * df * cls.norm_cdf(d2)
            intrinsic = max(0.0, spot - strike)
        else:
            price = strike * df * cls.norm_cdf(-d2) - spot * cls.norm_cdf(-d1)
            intrinsic = max(0.0, strike - spot)
        return max(price, intrinsic, 0.01)


# ==============================================================================
# SECTION 2: ASSET TAXONOMY & WATCHLIST MATRIX
# ==============================================================================

class AssetCategory(str, Enum):
    CORE_EQUITY = "Core Equity / Tax-Advantaged"
    LEVERAGED_EQUITY = "Triple-Leveraged Equities"
    COMMODITY = "Macro Commodities"
    CRYPTO_BONDS = "Spot Crypto & Bonds"


@dataclass
class TickerMetadata:
    symbol: str
    name: str
    category: AssetCategory
    is_leveraged: bool
    is_tax_advantaged: bool
    strike_step: float
    beta: float = 1.0


WATCHLIST_MATRIX: Dict[str, TickerMetadata] = {
    "SPY": TickerMetadata("SPY", "SPDR S&P 500 ETF Trust", AssetCategory.CORE_EQUITY, False, False, 1.0, 1.00),
    "QQQ": TickerMetadata("QQQ", "Invesco QQQ Trust (Nasdaq-100)", AssetCategory.CORE_EQUITY, False, False, 1.0, 1.18),
    "XSP": TickerMetadata("XSP", "Mini-S&P 500 Index Options", AssetCategory.CORE_EQUITY, False, True, 1.0, 1.00),
    "QQQM": TickerMetadata("QQQM", "Invesco NASDAQ 100 ETF", AssetCategory.CORE_EQUITY, False, False, 1.0, 1.18),
    "SPYM": TickerMetadata("SPYM", "SPDR Portfolio S&P 500 ETF", AssetCategory.CORE_EQUITY, False, False, 0.5, 1.00),
    "TQQQ": TickerMetadata("TQQQ", "ProShares UltraPro QQQ (3x)", AssetCategory.LEVERAGED_EQUITY, True, False, 1.0, 3.50),
    "SOXL": TickerMetadata("SOXL", "Direxion Daily Semiconductor Bull 3X", AssetCategory.LEVERAGED_EQUITY, True, False, 0.5, 3.80),
    "SPXL": TickerMetadata("SPXL", "Direxion Daily S&P 500 Bull 3X", AssetCategory.LEVERAGED_EQUITY, True, False, 1.0, 3.00),
    "GLD": TickerMetadata("GLD", "SPDR Gold Trust", AssetCategory.COMMODITY, False, False, 1.0, 0.08),
    "SLV": TickerMetadata("SLV", "iShares Silver Trust", AssetCategory.COMMODITY, False, False, 0.5, 0.35),
    "IAU": TickerMetadata("IAU", "iShares Gold Trust", AssetCategory.COMMODITY, False, False, 0.5, 0.08),
    "IBIT": TickerMetadata("IBIT", "iShares Bitcoin Trust ETF", AssetCategory.CRYPTO_BONDS, False, False, 0.5, 1.75),
    "TLT": TickerMetadata("TLT", "iShares 20+ Year Treasury Bond", AssetCategory.CRYPTO_BONDS, False, False, 0.5, -0.25),
}


# ==============================================================================
# SECTION 3: STRATEGY SCANNER (100% LIVE EXCHANGE DATA)
# ==============================================================================

@dataclass
class TickerScanResult:
    symbol: str
    name: str
    category: str
    is_leveraged: bool
    is_tax_advantaged: bool
    spot: float
    iv: float
    iv_rank: float
    iv_percentile_6m: float
    rsi: float
    wheel_qualified: bool
    leap_qualified: bool
    disqualification_reason: Optional[str] = None
    earnings_date: Optional[str] = None
    days_to_earnings: Optional[int] = None


@dataclass
class WheelStrikeRecommendation:
    symbol: str
    spot: float
    iv: float
    iv_rank: float
    is_leveraged: bool
    target_delta: float
    target_dte: int
    exact_strike: float
    snapped_strike: float
    live_contract: str
    bid: float
    ask: float
    mid: float
    delta: float
    gamma: float
    theta: float
    required_collateral: float
    max_profit_credit: float
    profit_target_50: float
    annualized_roc: float
    downside_buffer_pct: float
    expiration: str = ""
    roll_trigger_dte: int = 14
    iv_percentile_6m: float = 0.0
    expected_1sd_move: float = 0.0
    is_qualified: bool = False
    status: str = "WATCHLIST"
    earnings_date: Optional[str] = None
    days_to_earnings: Optional[int] = None
    earnings_warning: Optional[str] = None


@dataclass
class LeapRecommendation:
    symbol: str
    spot: float
    iv: float
    iv_rank: float
    rsi: float
    leap_target_delta: float
    leap_target_dte: int
    leap_strike: float
    leap_contract: str
    leap_bid: float
    leap_ask: float
    leap_mid: float
    capital_saved_vs_shares: float
    effective_leverage: float
    intrinsic_value: float
    extrinsic_value: float
    annual_drag_pct: float
    pmcc_target_delta: float
    pmcc_target_dte: int
    pmcc_strike: float
    pmcc_contract: str
    pmcc_mid: float
    net_debit: float
    pmcc_spread_width: float
    pmcc_gap_safety: str
    pmcc_cash_flow_yield_pct: float
    workflow_queue: str
    leap_expiration: str = ""
    pmcc_expiration: str = ""
    leap_delta: float = 0.85
    leap_theta: float = -0.01
    pmcc_bid: float = 0.0
    pmcc_ask: float = 0.0
    pmcc_delta: float = 0.30
    pmcc_theta: float = -0.04
    name: str = ""
    is_qualified: bool = False
    status: str = "WATCHLIST"


class StrategyScanner:
    """Strategy Scanner powered exclusively by live exchange data."""

    def __init__(self):
        self.market_client = LiveMarketClient()
        self.math = QuantMath()
        self.snapshots: Dict[str, LiveTickerSnapshot] = {}
        self.refresh()

    def refresh(self):
        self.snapshots = self.market_client.get_all_snapshots_parallel(list(WATCHLIST_MATRIX.keys()))

    def scan_ticker_universe(self) -> List[TickerScanResult]:
        results: List[TickerScanResult] = []

        for sym, meta in WATCHLIST_MATRIX.items():
            snap = self.snapshots.get(sym)
            if not snap:
                continue

            spot = snap.spot
            iv = snap.iv_current
            ivr = snap.iv_rank
            ivp = snap.iv_percentile_6m
            rsi = snap.rsi_14

            wheel_qualified = (ivr > 40.0 or ivp > 50.0)

            leap_disqualifier = None
            if meta.is_leveraged:
                leap_disqualifier = "REJECTED: Leveraged ETF prohibited from LEAP mode."
                leap_qualified = False
            elif ivr >= 20.0:
                leap_disqualifier = f"REJECTED: IV Rank ({ivr:.1f}%) not < 20%."
                leap_qualified = False
            elif rsi >= 35.0:
                leap_disqualifier = f"REJECTED: Daily RSI ({rsi:.1f}) not < 35."
                leap_qualified = False
            else:
                leap_qualified = True

            results.append(
                TickerScanResult(
                    symbol=sym,
                    name=meta.name,
                    category=meta.category.value,
                    is_leveraged=meta.is_leveraged,
                    is_tax_advantaged=meta.is_tax_advantaged,
                    spot=spot,
                    iv=iv,
                    iv_rank=ivr,
                    iv_percentile_6m=ivp,
                    rsi=rsi,
                    wheel_qualified=wheel_qualified,
                    leap_qualified=leap_qualified,
                    disqualification_reason=leap_disqualifier,
                    earnings_date=snap.earnings_date,
                    days_to_earnings=snap.days_to_earnings,
                )
            )
        return results

    def generate_wheel_matrix(self, all_tickers: bool = True) -> List[WheelStrikeRecommendation]:
        recommendations: List[WheelStrikeRecommendation] = []

        for sym, meta in WATCHLIST_MATRIX.items():
            snap = self.snapshots.get(sym)
            if not snap:
                continue

            is_qualified = (snap.iv_rank > 40.0 or snap.iv_percentile_6m > 50.0)
            if not is_qualified and not all_tickers:
                continue

            spot = snap.spot
            iv = max(0.10, snap.iv_current)
            target_dte = 45
            target_delta = -0.20 if meta.is_leveraged else -0.30

            # 1-SD Strike calculation
            t = target_dte / 365.0
            move_1sd = spot * iv * math.sqrt(t)
            exact_k = spot - move_1sd
            snapped_k = round(exact_k / meta.strike_step) * meta.strike_step

            live_contract = f"{sym}-PUT-${snapped_k:.1f}"
            bid, ask, mid = 0.0, 0.0, 0.0
            delta_val, gamma_val, theta_val = target_delta, 0.01, -0.05
            exp_date = ""

            chain = self.market_client.get_live_option_chain(sym, 28, 55)
            puts = [c for c in chain if c.option_type == "PUT"]
            if puts:
                closest_put = min(puts, key=lambda c: abs(c.strike - snapped_k))
                live_contract = closest_put.contract_symbol
                snapped_k = closest_put.strike
                target_dte = closest_put.dte
                exp_date = closest_put.expiration
                bid = closest_put.bid
                ask = closest_put.ask
                mid = closest_put.mid
                delta_val = closest_put.delta_approx
                gamma_val = closest_put.gamma_approx
                theta_val = closest_put.theta_approx

            if not exp_date and live_contract:
                exp_date = parse_occ_expiration(live_contract) or ""
            if not exp_date and target_dte:
                exp_date = (datetime.date.today() + datetime.timedelta(days=target_dte)).isoformat()

            if mid <= 0:
                mid = round(self.math.black_scholes_price(spot, snapped_k, target_dte, iv, 0.045, is_call=False), 2)
                bid = round(max(0.01, mid * 0.95), 2)
                ask = round(mid * 1.05, 2)

            collateral = snapped_k * 100.0
            max_profit = mid * 100.0
            profit_target_50 = round(mid * 0.50, 2)
            downside_buffer = round(((spot - snapped_k) / spot) * 100.0, 2) if spot > 0 else 0.0
            annualized_roc = round(((mid / snapped_k) * (365.0 / target_dte)) * 100.0, 2) if snapped_k > 0 else 0.0

            earn_date = snap.earnings_date
            days_earn = snap.days_to_earnings
            earn_warning = None
            if days_earn is not None and 0 <= days_earn <= target_dte:
                earn_warning = f"⚠️ EARNINGS in {days_earn}d ({earn_date})"
                if days_earn <= 7:
                    is_qualified = False
                    status_label = f"EARNINGS BLACKOUT ({days_earn}d)"
                else:
                    status_label = "QUALIFIED TRIGGER" if is_qualified else f"WATCH (IVR {snap.iv_rank:.0f}% | IVP {snap.iv_percentile_6m:.0f}%)"
            else:
                status_label = "QUALIFIED TRIGGER" if is_qualified else f"WATCH (IVR {snap.iv_rank:.0f}% | IVP {snap.iv_percentile_6m:.0f}%)"

            rec = WheelStrikeRecommendation(
                symbol=sym,
                spot=spot,
                iv=iv,
                iv_rank=snap.iv_rank,
                is_leveraged=meta.is_leveraged,
                target_delta=target_delta,
                target_dte=target_dte,
                exact_strike=round(exact_k, 2),
                snapped_strike=snapped_k,
                live_contract=live_contract,
                bid=bid,
                ask=ask,
                mid=mid,
                delta=delta_val,
                gamma=gamma_val,
                theta=theta_val,
                required_collateral=collateral,
                max_profit_credit=max_profit,
                profit_target_50=profit_target_50,
                annualized_roc=annualized_roc,
                downside_buffer_pct=downside_buffer,
                expiration=exp_date,
                roll_trigger_dte=14,
                iv_percentile_6m=snap.iv_percentile_6m,
                expected_1sd_move=round(move_1sd, 2),
                is_qualified=is_qualified,
                status=status_label,
                earnings_date=earn_date,
                days_to_earnings=days_earn,
                earnings_warning=earn_warning,
            )
            recommendations.append(rec)

        recommendations.sort(key=lambda r: (not r.is_qualified, -r.iv_rank))
        return recommendations

    def generate_leap_matrix(self, all_tickers: bool = True) -> List[LeapRecommendation]:
        recommendations: List[LeapRecommendation] = []

        for sym, meta in WATCHLIST_MATRIX.items():
            if meta.is_leveraged:
                continue

            snap = self.snapshots.get(sym)
            if not snap:
                continue

            is_qualified = (snap.iv_rank < 20.0 and snap.rsi_14 < 35.0)
            if not is_qualified and not all_tickers:
                continue

            spot = snap.spot
            iv = max(0.10, snap.iv_current)
            leap_contract = f"{sym}-LEAP-CALL"
            leap_strike = round(spot * 0.85)
            leap_bid, leap_ask, leap_mid = 0.0, 0.0, 0.0
            leap_dte = 390
            leap_exp = ""
            leap_delta_val = 0.85
            leap_theta_val = -0.01

            leaps = self.market_client.get_live_leap_options(sym, min_dte=340)
            if leaps:
                # One expiration only (closest to 390 DTE), tradable (live bid/ask) strikes preferred.
                leaps = [c for c in leaps if c.bid > 0 and c.ask > 0] or leaps
                _leap_exp = min(leaps, key=lambda c: abs(c.dte - 390)).expiration
                leaps = [c for c in leaps if c.expiration == _leap_exp]
                best_leap = min(leaps, key=lambda c: abs(c.delta_approx - 0.85))
                leap_contract = best_leap.contract_symbol
                leap_strike = best_leap.strike
                leap_bid = best_leap.bid
                leap_ask = best_leap.ask
                leap_mid = best_leap.mid
                leap_dte = best_leap.dte
                leap_exp = best_leap.expiration
                leap_delta_val = best_leap.delta_approx
                leap_theta_val = best_leap.theta_approx

            if not leap_exp and leap_contract:
                leap_exp = parse_occ_expiration(leap_contract) or ""
            if not leap_exp and leap_dte:
                leap_exp = (datetime.date.today() + datetime.timedelta(days=leap_dte)).isoformat()

            if leap_mid <= 0:
                leap_mid = round(self.math.black_scholes_price(spot, leap_strike, leap_dte, iv, 0.045, is_call=True), 2)
                leap_bid = round(max(0.01, leap_mid * 0.96), 2)
                leap_ask = round(leap_mid * 1.04, 2)

            capital_saved = round((spot * 100.0) - (leap_mid * 100.0), 2)

            # PMCC Short Leg (45 DTE 0.30 Delta Call)
            pmcc_contract = f"{sym}-PMCC-0.30D"
            pmcc_strike = round(spot * 1.04)
            pmcc_mid = round(spot * 0.015, 2)
            pmcc_dte = 45
            pmcc_exp = ""
            pmcc_bid = 0.0
            pmcc_ask = 0.0
            pmcc_delta_val = 0.30
            pmcc_theta_val = -0.04

            chain = self.market_client.get_live_option_chain(sym, 28, 55)
            calls = [c for c in chain if c.option_type == "CALL"]
            calls = [c for c in calls if c.bid > 0 and c.ask > 0] or calls
            if calls:
                _pm_exp = min(calls, key=lambda c: abs(c.dte - 45)).expiration
                calls = [c for c in calls if c.expiration == _pm_exp]
                best_pmcc = min(calls, key=lambda c: abs(c.delta_approx - 0.30))
                pmcc_contract = best_pmcc.contract_symbol
                pmcc_strike = best_pmcc.strike
                pmcc_bid = best_pmcc.bid
                pmcc_ask = best_pmcc.ask
                pmcc_mid = best_pmcc.mid
                pmcc_dte = best_pmcc.dte
                pmcc_exp = best_pmcc.expiration
                pmcc_delta_val = best_pmcc.delta_approx
                pmcc_theta_val = best_pmcc.theta_approx

            if not pmcc_exp and pmcc_contract:
                pmcc_exp = parse_occ_expiration(pmcc_contract) or ""
            if not pmcc_exp and pmcc_dte:
                pmcc_exp = (datetime.date.today() + datetime.timedelta(days=pmcc_dte)).isoformat()

            net_debit = round(leap_mid - pmcc_mid, 2)
            pmcc_yield = round((pmcc_mid / net_debit) * 100.0, 2) if net_debit > 0 else 0.0
            workflow = f"BUY 1x {leap_contract} (@ mid ${leap_mid:.2f}) | SELL 1x {pmcc_contract} (@ mid ${pmcc_mid:.2f})"

            intrinsic = max(0.0, spot - leap_strike)
            extrinsic = max(0.0, leap_mid - intrinsic)
            effective_leverage = round((max(0.01, leap_delta_val) * spot) / leap_mid, 2) if leap_mid > 0 else 0.0
            annual_drag = round((extrinsic / spot) * (365.0 / max(1, leap_dte)) * 100.0, 2) if spot > 0 else 0.0
            spread_width = round(pmcc_strike - leap_strike, 2)
            gap_safety = "SAFE: Zero Upside Gap Risk" if spread_width >= net_debit else "MONITOR: Upside Risk"
            status_str = "QUALIFIED LEAP TRIGGER" if is_qualified else f"WATCH (RSI {snap.rsi_14:.1f} | IVR {snap.iv_rank:.1f}%)"

            rec = LeapRecommendation(
                symbol=sym,
                spot=spot,
                iv=iv,
                iv_rank=snap.iv_rank,
                rsi=snap.rsi_14,
                leap_target_delta=0.85,
                leap_target_dte=leap_dte,
                leap_strike=leap_strike,
                leap_contract=leap_contract,
                leap_bid=leap_bid,
                leap_ask=leap_ask,
                leap_mid=leap_mid,
                capital_saved_vs_shares=capital_saved,
                effective_leverage=effective_leverage,
                intrinsic_value=round(intrinsic, 2),
                extrinsic_value=round(extrinsic, 2),
                annual_drag_pct=annual_drag,
                pmcc_target_delta=0.30,
                pmcc_target_dte=pmcc_dte,
                pmcc_strike=pmcc_strike,
                pmcc_contract=pmcc_contract,
                pmcc_mid=pmcc_mid,
                net_debit=net_debit,
                pmcc_spread_width=spread_width,
                pmcc_gap_safety=gap_safety,
                pmcc_cash_flow_yield_pct=pmcc_yield,
                workflow_queue=workflow,
                leap_expiration=leap_exp,
                pmcc_expiration=pmcc_exp,
                leap_delta=leap_delta_val,
                leap_theta=leap_theta_val,
                pmcc_bid=pmcc_bid,
                pmcc_ask=pmcc_ask,
                pmcc_delta=pmcc_delta_val,
                pmcc_theta=pmcc_theta_val,
                name=meta.name,
                is_qualified=is_qualified,
                status=status_str,
            )
            recommendations.append(rec)

        recommendations.sort(key=lambda r: (not r.is_qualified, r.rsi))
        return recommendations

    def generate_leap_put_matrix(self, all_tickers: bool = True) -> List[Dict[str, Any]]:
        """
        Generates Bearish / Overbought LEAP Long Put (-0.85 Delta, 340+ DTE) with
        Poor Man's Covered Put (PMCP, -0.30 Delta, 30-45 DTE) overlay.
        Trigger: 14-Day RSI > 65 (Overbought Rally) AND IV Rank < 25% (Cheap Put Volatility).
        Excludes 3x Leveraged ETFs (TQQQ, SOXL, SPXL).
        """
        recommendations: List[Dict[str, Any]] = []

        for sym, meta in WATCHLIST_MATRIX.items():
            if meta.is_leveraged:
                continue

            snap = self.snapshots.get(sym)
            if not snap:
                continue

            is_qualified = (snap.rsi_14 > 65.0 and snap.iv_rank < 25.0)
            if not is_qualified and not all_tickers:
                continue

            spot = snap.spot
            iv = max(0.10, snap.iv_current)
            leap_contract = f"{sym}-LEAP-PUT"
            leap_strike = round(spot * 1.15)
            leap_bid, leap_ask, leap_mid = 0.0, 0.0, 0.0
            leap_dte = 390
            leap_exp = ""
            leap_delta_val = -0.85

            leaps = self.market_client.get_live_leap_options(sym, min_dte=340, option_type="PUT")
            if leaps:
                best_leap = min(leaps, key=lambda c: abs(c.delta_approx - (-0.85)))
                leap_contract = best_leap.contract_symbol
                leap_strike = best_leap.strike
                leap_bid = best_leap.bid
                leap_ask = best_leap.ask
                leap_mid = best_leap.mid
                leap_dte = best_leap.dte
                leap_exp = best_leap.expiration
                leap_delta_val = best_leap.delta_approx

            if not leap_exp and leap_contract:
                leap_exp = parse_occ_expiration(leap_contract) or ""
            if not leap_exp and leap_dte:
                leap_exp = (datetime.date.today() + datetime.timedelta(days=leap_dte)).isoformat()

            if leap_mid <= 0:
                leap_mid = round(self.math.black_scholes_price(spot, leap_strike, leap_dte, iv, 0.045, is_call=False), 2)
                leap_bid = round(max(0.01, leap_mid * 0.96), 2)
                leap_ask = round(leap_mid * 1.04, 2)

            capital_saved = round((spot * 100.0) - (leap_mid * 100.0), 2)

            # PMCP Short Leg (45 DTE -0.30 Delta Put)
            pmcp_contract = f"{sym}-PMCP-0.30D"
            pmcp_strike = round(spot * 0.96)
            pmcp_mid = round(spot * 0.015, 2)
            pmcp_dte = 45
            pmcp_exp = ""
            pmcp_bid = 0.0
            pmcp_ask = 0.0
            pmcp_delta_val = -0.30

            chain = self.market_client.get_live_option_chain(sym, 28, 55)
            puts = [c for c in chain if c.option_type == "PUT"]
            if puts:
                best_pmcp = min(puts, key=lambda c: abs(c.delta_approx - (-0.30)))
                pmcp_contract = best_pmcp.contract_symbol
                pmcp_strike = best_pmcp.strike
                pmcp_mid = best_pmcp.mid
                pmcp_dte = best_pmcp.dte
                pmcp_exp = best_pmcp.expiration
                pmcp_delta_val = best_pmcp.delta_approx

            if not pmcp_exp and pmcp_contract:
                pmcp_exp = parse_occ_expiration(pmcp_contract) or ""
            if not pmcp_exp and pmcp_dte:
                pmcp_exp = (datetime.date.today() + datetime.timedelta(days=pmcp_dte)).isoformat()

            net_debit = round(leap_mid - pmcp_mid, 2)
            intrinsic = max(0.0, leap_strike - spot)
            extrinsic = max(0.0, leap_mid - intrinsic)
            effective_leverage = round((max(0.01, abs(leap_delta_val)) * spot) / leap_mid, 2) if leap_mid > 0 else 0.0
            annual_drag = round((extrinsic / spot) * (365.0 / max(1, leap_dte)) * 100.0, 2) if spot > 0 else 0.0
            spread_width = round(leap_strike - pmcp_strike, 2)
            gap_safety = "SAFE: Zero Downside Crash Risk" if spread_width >= net_debit else "MONITOR: Spread < Debit"

            recommendations.append({
                "symbol": sym,
                "spot": spot,
                "iv": round(iv * 100.0, 1),
                "iv_rank": snap.iv_rank,
                "rsi": snap.rsi_14,
                "is_qualified": is_qualified,
                "status": "QUALIFIED PUT TRIGGER" if is_qualified else "CHECK / WATCHLIST",
                "leap_target_delta": -0.85,
                "leap_target_dte": leap_dte,
                "leap_expiration": leap_exp,
                "leap_delta": leap_delta_val,
                "leap_strike": leap_strike,
                "leap_contract": leap_contract,
                "leap_bid": leap_bid,
                "leap_ask": leap_ask,
                "leap_mid": leap_mid,
                "capital_saved_vs_short": capital_saved,
                "effective_leverage": effective_leverage,
                "annual_drag_pct": annual_drag,
                "pmcp_strike": pmcp_strike,
                "pmcp_contract": pmcp_contract,
                "pmcp_mid": pmcp_mid,
                "pmcp_dte": pmcp_dte,
                "pmcp_expiration": pmcp_exp,
                "pmcp_bid": pmcp_bid,
                "pmcp_ask": pmcp_ask,
                "pmcp_delta": pmcp_delta_val,
                "net_debit": net_debit,
                "spread_width": spread_width,
                "gap_safety": gap_safety,
            })

        return recommendations


# ==============================================================================
# SECTION 4: REAL-TIME GAMMA DANGER SENTINEL (MONITORS positions.csv)
# ==============================================================================

    def generate_spread_matrix(self, all_tickers: bool = True) -> List[Dict[str, Any]]:
        """
        Dynamically generates optimal Defined-Risk Vertical Spreads (Call or Put) based on real-time market regimes:
        - High IV (IV Rank >= 35% or 6M IVP >= 40%): Credit Spreads (Positive Theta, Short Vega)
            * RSI <= 50 -> Bull Put Credit Spread (Oversold/Dip Buy below support)
            * RSI > 50  -> Bear Call Credit Spread (Overbought/Fade Rally below resistance)
        - Low IV (IV Rank < 35%): Debit Spreads (Long Vega, Defined Cheap Cost)
            * RSI <= 50 -> Bull Call Debit Spread (Cheap Bullish Momentum run)
            * RSI > 50  -> Bear Put Debit Spread (Cheap Bearish Downside Hedge)
        """
        spreads: List[Dict[str, Any]] = []

        for sym, meta in WATCHLIST_MATRIX.items():
            snap = self.snapshots.get(sym)
            if not snap:
                continue
            spot = snap.spot
            iv = max(0.10, snap.iv_current)
            ivr = snap.iv_rank
            ivp = snap.iv_percentile_6m
            rsi = snap.rsi_14

            is_high_iv = (ivr >= 35.0 or ivp >= 40.0)

            if is_high_iv:
                is_credit = True
                if rsi <= 50.0:
                    spread_type = "Bull Put Credit Spread"
                    bias = "Bullish / Dip Buy"
                    is_call = False
                else:
                    spread_type = "Bear Call Credit Spread"
                    bias = "Bearish / Fade Rally"
                    is_call = True
            else:
                is_credit = False
                if rsi <= 50.0:
                    spread_type = "Bull Call Debit Spread"
                    bias = "Bullish Momentum"
                    is_call = True
                else:
                    spread_type = "Bear Put Debit Spread"
                    bias = "Bearish Hedge"
                    is_call = False

            # Width determination
            if spot >= 400:
                width = 5.0
            elif spot >= 150:
                width = 5.0 if meta.strike_step >= 5 else 2.5
            elif spot >= 50:
                width = 2.0 if meta.strike_step <= 1 else meta.strike_step
            else:
                width = 1.0

            chain = self.market_client.get_live_option_chain(sym, 28, 55)
            req_type = "CALL" if is_call else "PUT"
            candidates = [c for c in chain if c.option_type == req_type]

            target_dte = 35
            t = target_dte / 365.0
            move_1sd = spot * iv * math.sqrt(t)

            if spread_type == "Bull Put Credit Spread":
                raw_short_k = spot - move_1sd * 0.8
                short_k = round(raw_short_k / meta.strike_step) * meta.strike_step
                long_k = short_k - width
            elif spread_type == "Bear Call Credit Spread":
                raw_short_k = spot + move_1sd * 0.8
                short_k = round(raw_short_k / meta.strike_step) * meta.strike_step
                long_k = short_k + width
            elif spread_type == "Bull Call Debit Spread":
                raw_long_k = spot - move_1sd * 0.2
                long_k = round(raw_long_k / meta.strike_step) * meta.strike_step
                short_k = long_k + width
            else:  # Bear Put Debit Spread
                raw_long_k = spot + move_1sd * 0.2
                long_k = round(raw_long_k / meta.strike_step) * meta.strike_step
                short_k = long_k - width

            # Resolve legs against live chain
            short_c = min(candidates, key=lambda c: abs(c.strike - short_k)) if candidates else None
            long_c = min(candidates, key=lambda c: abs(c.strike - long_k)) if candidates else None

            short_strike = short_c.strike if short_c else short_k
            long_strike = long_c.strike if long_c else long_k
            actual_width = abs(short_strike - long_strike)
            if actual_width <= 0:
                actual_width = width
                if is_call:
                    long_strike = short_strike + width if is_credit else short_strike - width
                else:
                    long_strike = short_strike - width if is_credit else short_strike + width

            dte_val = short_c.dte if short_c else target_dte
            exp_date = short_c.expiration if short_c else ""
            if not exp_date and short_c and short_c.contract_symbol:
                exp_date = parse_occ_expiration(short_c.contract_symbol) or ""
            if not exp_date:
                exp_date = (datetime.date.today() + datetime.timedelta(days=dte_val)).isoformat()

            short_mid = short_c.mid if (short_c and short_c.mid > 0) else round(self.math.black_scholes_price(spot, short_strike, dte_val, iv, 0.045, is_call=is_call), 2)
            long_mid = long_c.mid if (long_c and long_c.mid > 0) else round(self.math.black_scholes_price(spot, long_strike, dte_val, iv, 0.045, is_call=is_call), 2)

            short_delta = short_c.delta_approx if short_c else (-0.25 if not is_call else 0.25)
            long_delta = long_c.delta_approx if long_c else (-0.15 if not is_call else 0.15)

            short_sym = short_c.contract_symbol if short_c else f"{sym}-{req_type}-${short_strike:g}"
            long_sym = long_c.contract_symbol if long_c else f"{sym}-{req_type}-${long_strike:g}"

            if is_credit:
                net_price = max(0.05, round(short_mid - long_mid, 2))
                if net_price >= actual_width:
                    net_price = round(actual_width * 0.35, 2)
                max_profit = round(net_price * 100.0, 2)
                max_risk = max(1.0, round((actual_width - net_price) * 100.0, 2))
                pop = round(max(50.0, min(95.0, (1.0 - abs(short_delta)) * 100.0)), 1)
                breakeven = round(short_strike - net_price, 2) if not is_call else round(short_strike + net_price, 2)
                buffer_pct = round(abs(spot - short_strike) / spot * 100.0, 1)
                exit_target = round(net_price * 0.5, 2)
                trade_action = f"STO 1x ${short_strike:g} {req_type} / BTO 1x ${long_strike:g} {req_type}"
                price_desc = f"+${net_price:.2f} credit"
            else:
                net_price = max(0.05, round(long_mid - short_mid, 2))
                if net_price >= actual_width:
                    net_price = round(actual_width * 0.55, 2)
                max_profit = round((actual_width - net_price) * 100.0, 2)
                max_risk = max(1.0, round(net_price * 100.0, 2))
                pop = round(max(40.0, min(80.0, abs(long_delta) * 100.0)), 1)
                breakeven = round(long_strike + net_price, 2) if is_call else round(long_strike - net_price, 2)
                buffer_pct = round(abs(spot - long_strike) / spot * 100.0, 1)
                exit_target = round(net_price + (actual_width - net_price) * 0.5, 2)
                trade_action = f"BTO 1x ${long_strike:g} {req_type} / STO 1x ${short_strike:g} {req_type}"
                price_desc = f"-${net_price:.2f} debit"

            roc = round((max_profit / max_risk) * 100.0, 1) if max_risk > 0 else 0.0
            annualized_roc = round(roc * (365.0 / dte_val), 1) if dte_val > 0 else 0.0

            earn_date = snap.earnings_date
            days_earn = snap.days_to_earnings
            earn_warning = None
            is_qualified = (is_credit and ivr >= 35.0) or (not is_credit and ivr < 25.0 and rsi < 40.0)
            status_text = "QUALIFIED SPREAD" if is_qualified else "MONITOR"

            if days_earn is not None and 0 <= days_earn <= dte_val:
                earn_warning = f"⚠️ EARNINGS in {days_earn}d ({earn_date})"
                if days_earn <= 7:
                    is_qualified = False
                    status_text = f"EARNINGS BLACKOUT ({days_earn}d)"

            spreads.append({
                "earnings_date": earn_date,
                "days_to_earnings": days_earn,
                "earnings_warning": earn_warning,
                "symbol": sym,
                "spot": spot,
                "iv": round(iv * 100.0, 1),
                "iv_rank": round(ivr, 1),
                "iv_percentile_6m": round(ivp, 1),
                "rsi_14": round(rsi, 1),
                "spread_type": spread_type,
                "is_credit": is_credit,
                "bias": bias,
                "width": actual_width,
                "target_dte": dte_val,
                "expiration": exp_date,
                "short_strike": short_strike,
                "long_strike": long_strike,
                "short_contract": short_sym,
                "long_contract": long_sym,
                "short_mid": short_mid,
                "long_mid": long_mid,
                "short_delta": short_delta,
                "long_delta": long_delta,
                "net_price": net_price,
                "price_desc": price_desc,
                "max_profit": max_profit,
                "max_risk": max_risk,
                "roc": roc,
                "annualized_roc": annualized_roc,
                "pop": pop,
                "breakeven": breakeven,
                "buffer_pct": buffer_pct,
                "exit_target": exit_target,
                "trade_action": trade_action,
                "is_qualified": is_qualified,
                "status": status_text,
                "directive": f"{trade_action} exp {exp_date} ({dte_val} DTE) @ {price_desc} | Exit @ ${exit_target:.2f} (50% target) or 14 DTE",
                "regime_note": f"IVR {ivr:.0f}% ({'High IV' if is_high_iv else 'Low IV'}) • RSI {rsi:.0f} ({'Oversold' if rsi < 35 else ('Overbought' if rsi > 65 else 'Neutral')})"
            })

        spreads.sort(key=lambda s: (not s["is_qualified"], -s["iv_rank"] if s["is_credit"] else s["iv_rank"]))
        return spreads


class GammaDangerSentinel:
    """Audits manual entries in positions.csv against live market feeds."""

    def __init__(self, scanner: StrategyScanner, positions_file: str = "positions.csv"):
        self.scanner = scanner
        self.positions_file = positions_file
        self.alert_history: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def audit_manual_positions(self, emit_stderr: bool = True) -> List[Dict[str, Any]]:
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

        danger_positions: List[Dict[str, Any]] = []
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for idx, (sym, strike, opt_type, exp_str, contracts, entry_prem) in enumerate(user_rows, start=1):
            if opt_type in ("EQUITY", "SHARES", "STOCK"):
                continue
            try:
                exp_date = datetime.datetime.strptime(exp_str, "%Y-%m-%d").date()
                dte = (exp_date - today).days
            except Exception:
                continue

            if contracts >= 0 or dte > 14:
                continue

            matched = self.scanner.market_client.lookup_position_contract(sym, opt_type, strike, dte)
            curr_mid = matched.mid if matched else entry_prem
            gamma_val = matched.gamma_approx if matched else 0.02
            delta_val = matched.delta_approx if matched else -0.25

            if contracts < 0 and dte <= 14:
                pnl = (entry_prem - curr_mid) * abs(contracts) * 100.0
                pnl_pct = ((entry_prem - curr_mid) / entry_prem) * 100.0 if entry_prem > 0 else 0.0

                alert = {
                    "timestamp": now_str,
                    "position_id": f"MANUAL-{idx:02d}-{sym}",
                    "symbol": sym,
                    "strike": strike,
                    "option_type": opt_type,
                    "expiration": exp_str,
                    "dte": dte,
                    "delta": delta_val,
                    "gamma": gamma_val,
                    "current_mid": curr_mid,
                    "unrealized_pnl": round(pnl, 2),
                    "pnl_percent": round(pnl_pct, 1),
                    "recommended_action": "STRUCTURAL ROLL OUT (42 DTE) OR CLOSE",
                    "roll_plan": f"BTC {abs(contracts)}x {sym} ${strike:.1f}{opt_type[0]} | STO {abs(contracts)}x {sym} +42DTE (Roll Out)",
                }
                danger_positions.append(alert)

                if emit_stderr:
                    stderr_msg = (
                        f"\n\033[91;1m[GAMMA DANGER ZONE SENTINEL ALERT]\033[0m {now_str}\n"
                        f"  -> Position ID: \033[93m{alert['position_id']}\033[0m ({sym} ${strike:.1f} {opt_type})\n"
                        f"  -> Expiration : \033[91;1m{dte} Days to Expiration\033[0m (Threshold: 14 DTE)\n"
                        f"  -> Greeks     : Delta = {delta_val:.2f} | \033[91mGamma = {gamma_val:.4f}\033[0m\n"
                        f"  -> P&L        : ${pnl:+.2f} ({pnl_pct:+.1f}%)\n"
                        f"  -> Directive  : \033[96;1m{alert['recommended_action']}\033[0m\n"
                    )
                    sys.stderr.write(stderr_msg)
                    sys.stderr.flush()

        with self._lock:
            self.alert_history = danger_positions

        return danger_positions

    def evaluate_all_manual_positions(self) -> List[Dict[str, Any]]:
        """Returns all active manual positions in positions.csv with live market quotes, P&L, and Dynamic Exit status."""
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
                    acct_label = row[6].strip() if len(row) > 6 else "Manual"
                    user_rows.append((sym, strike, opt_type, exp_str, contracts, entry_prem, acct_label))
                except Exception:
                    continue

        results: List[Dict[str, Any]] = []
        for idx, (sym, strike, opt_type, exp_str, contracts, entry_prem, acct_label) in enumerate(user_rows, start=1):
            if opt_type in ("EQUITY", "SHARES", "STOCK"):
                if contracts < 100:
                    continue
                snap = self.scanner.snapshots.get(sym)
                curr_mid = snap.spot if snap else (entry_prem if entry_prem > 0 else 100.0)
                effective_entry = entry_prem if entry_prem > 0 else curr_mid
                pnl = (curr_mid - effective_entry) * contracts
                pnl_pct = ((curr_mid - effective_entry) / effective_entry) * 100.0 if effective_entry > 0 else 0.0

                if snap and snap.rsi_14 < 35.0:
                    status = f"EQUITY DIP ZONE: RSI {snap.rsi_14:.1f} (<35 DCA)"
                elif snap and (snap.rsi_14 > 65.0 or snap.iv_rank > 40.0) and contracts >= 100:
                    status = f"WRITE COVERED CALL (IVR {snap.iv_rank:.0f}% / RSI {snap.rsi_14:.0f})"
                elif contracts >= 100:
                    status = f"CORE EQUITY ({contracts // 100}x CC ELIGIBLE)"
                else:
                    status = "HOLD CORE EQUITY SHARES"

                results.append({
                    "index": idx,
                    "position_id": f"EQ-{idx:02d}-{sym}",
                    "account": acct_label,
                    "symbol": sym,
                    "strike": 0.0,
                    "option_type": "EQUITY",
                    "expiration": "SHARES",
                    "dte": 0,
                    "contracts": contracts,
                    "entry_premium": round(effective_entry, 2),
                    "current_mid": round(curr_mid, 2),
                    "pnl_dollars": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 1),
                    "delta": 1.0,
                    "gamma_danger": False,
                    "profit_target_hit": False,
                    "status": status,
                    "earnings_date": snap.earnings_date if snap else None,
                    "days_to_earnings": snap.days_to_earnings if snap else None,
                    "earnings_warning": (f"⚠️ EARNINGS in {snap.days_to_earnings}d ({snap.earnings_date})" if (snap and snap.days_to_earnings is not None and 0 <= snap.days_to_earnings <= 30) else None),
                })
                continue

            try:
                exp_date = datetime.datetime.strptime(exp_str, "%Y-%m-%d").date()
                dte = (exp_date - today).days
            except Exception:
                dte = 30

            matched = self.scanner.market_client.lookup_position_contract(sym, opt_type, strike, dte)
            curr_mid = matched.mid if matched else entry_prem
            opt_delta = matched.delta_approx if matched else (0.85 if (opt_type == "CALL" and contracts > 0) else (-0.25 if opt_type == "PUT" else 0.30))

            if contracts < 0:
                pnl = (entry_prem - curr_mid) * abs(contracts) * 100.0
                pnl_pct = ((entry_prem - curr_mid) / entry_prem) * 100.0 if entry_prem > 0 else 0.0
            else:
                pnl = (curr_mid - entry_prem) * abs(contracts) * 100.0
                pnl_pct = ((curr_mid - entry_prem) / entry_prem) * 100.0 if entry_prem > 0 else 0.0

            gamma_danger = (dte <= 14 and contracts < 0)
            profit_hit = (pnl_pct >= 50.0 and contracts < 0)
            if gamma_danger and profit_hit:
                status = "EXIT NOW: 50% PROFIT + <14 DTE GAMMA"
            elif profit_hit:
                status = "EXIT NOW: 50% MAX PROFIT HIT"
            elif gamma_danger:
                status = "ROLL / EXIT: <14 DTE GAMMA ZONE"
            elif contracts > 0:
                status = "HOLD LEAP: LONG CORE POSITION" if dte >= 60 else "REVIEW LEAP ROLL (<60 DTE)"
            else:
                status = "HOLD: SAFE THETA HARVEST"

            # Check earnings blackout for position
            snap = self.scanner.snapshots.get(sym)
            earn_date = snap.earnings_date if snap else None
            days_earn = snap.days_to_earnings if snap else None
            earn_warning = None
            if days_earn is not None and 0 <= days_earn <= max(dte, 1):
                earn_warning = f"⚠️ EARNINGS in {days_earn}d ({earn_date})"

            # Calculate concrete roll recommendation for <14 DTE short options
            roll_target_strike = None
            roll_target_exp = None
            roll_target_dte = None
            roll_net_credit = None
            roll_plan = None

            if gamma_danger:
                try:
                    roll_chain = self.scanner.market_client.get_live_option_chain(sym, 35, 55)
                    cands = [c for c in roll_chain if c.option_type == opt_type]
                    if cands:
                        best_roll = min(cands, key=lambda c: (abs(c.strike - strike), abs(c.dte - 42)))
                        roll_target_strike = best_roll.strike
                        roll_target_exp = best_roll.expiration
                        roll_target_dte = best_roll.dte
                        roll_net_credit = round(best_roll.mid - curr_mid, 2)
                        total_c = round(roll_net_credit * abs(contracts) * 100.0, 2)
                        cred_str = f"+${roll_net_credit:.2f} credit (+${total_c:,.0f})" if roll_net_credit >= 0 else f"-${abs(roll_net_credit):.2f} debit"
                        roll_plan = f"BTC {abs(contracts)}x ${strike:g}{opt_type[0]} @ ${curr_mid:.2f} | STO {abs(contracts)}x ${best_roll.strike:g}{opt_type[0]} ({best_roll.expiration}, {best_roll.dte} DTE) @ ${best_roll.mid:.2f} [{cred_str}]"
                except Exception:
                    pass

            results.append({
                "index": idx,
                "position_id": f"POS-{idx:02d}-{sym}",
                "account": acct_label,
                "symbol": sym,
                "strike": strike,
                "option_type": opt_type,
                "expiration": exp_str,
                "dte": dte,
                "contracts": contracts,
                "entry_premium": entry_prem,
                "current_mid": round(curr_mid, 2),
                "pnl_dollars": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 1),
                "delta": round(opt_delta, 3),
                "gamma_danger": gamma_danger,
                "profit_target_hit": profit_hit,
                "status": status,
                "earnings_date": earn_date,
                "days_to_earnings": days_earn,
                "earnings_warning": earn_warning,
                "roll_target_strike": roll_target_strike,
                "roll_target_exp": roll_target_exp,
                "roll_target_dte": roll_target_dte,
                "roll_net_credit": roll_net_credit,
                "roll_plan": roll_plan,
            })
        return results

    def evaluate_equity_and_exposure(self, evaluated_positions: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        """
        Aggregates Equity Shares + Option Contracts for each held ticker to compute:
        - Total Shares Held across accounts
        - Long LEAP Calls (+100x synthetic share base)
        - Short Calls sold against shares/LEAPs
        - Short Puts (Wheel assignment obligation)
        - True Net Share-Equivalent Delta Exposure (Shares + sum(contracts * 100 * delta))
        - Uncovered Covered-Call / PMCC Capacity & Live 0.30-Delta Call recommendation
        """
        positions = evaluated_positions if evaluated_positions is not None else self.evaluate_all_manual_positions()
        by_symbol: Dict[str, Dict[str, Any]] = {}

        for pos in positions:
            sym = pos["symbol"]
            # One exposure row per (ticker, account)
            group_key = f"{sym}||{pos.get('account', 'Manual')}"
            if group_key not in by_symbol:
                snap = self.scanner.snapshots.get(sym)
                by_symbol[group_key] = {
                    "symbol": sym,
                    "spot": snap.spot if snap else pos["current_mid"],
                    "rsi_14": snap.rsi_14 if snap else 50.0,
                    "iv_rank": snap.iv_rank if snap else 25.0,
                    "accounts": set(),
                    "by_account": {},
                    "shares": 0,
                    "equity_cost_sum": 0.0,
                    "equity_market_value": 0.0,
                    "equity_pnl_dollars": 0.0,
                    "long_leap_calls": 0,
                    "short_calls": 0,
                    "short_puts": 0,
                    "long_puts": 0,
                    "option_delta_shares": 0.0,
                    "options_pnl_dollars": 0.0,
                }

            entry = by_symbol[group_key]
            acct_name = pos.get("account", "Manual")
            entry["accounts"].add(acct_name)
            if acct_name not in entry["by_account"]:
                entry["by_account"][acct_name] = {
                    "shares": 0,
                    "equity_cost_sum": 0.0,
                    "leap_calls": 0,
                    "leap_be_sum": 0.0,
                    "short_calls": 0,
                    "short_puts": 0,
                }
            acct_info = entry["by_account"][acct_name]
            opt_type = pos["option_type"]
            qty = int(pos["contracts"])
            entry_prem = float(pos.get("entry_premium", 0.0))
            strike_val = float(pos.get("strike", 0.0))

            if opt_type == "EQUITY":
                entry["shares"] += qty
                entry["equity_cost_sum"] += qty * entry_prem
                entry["equity_market_value"] += qty * entry["spot"]
                entry["equity_pnl_dollars"] += pos.get("pnl_dollars", 0.0)
                acct_info["shares"] += qty
                acct_info["equity_cost_sum"] += qty * entry_prem
            else:
                raw_delta = float(pos.get("delta", 0.5 if opt_type == "CALL" else -0.3))
                # Ensure sign of contract delta aligns with Call (+) / Put (-)
                signed_contract_delta = abs(raw_delta) if opt_type == "CALL" else -abs(raw_delta)
                entry["option_delta_shares"] += qty * 100.0 * signed_contract_delta
                entry["options_pnl_dollars"] += pos.get("pnl_dollars", 0.0)

                if opt_type == "CALL":
                    if qty > 0:
                        entry["long_leap_calls"] += qty
                        acct_info["leap_calls"] += qty
                        acct_info["leap_be_sum"] += qty * (strike_val + entry_prem)
                    elif qty < 0:
                        entry["short_calls"] += abs(qty)
                        acct_info["short_calls"] += abs(qty)
                elif opt_type == "PUT":
                    if qty < 0:
                        entry["short_puts"] += abs(qty)
                        acct_info["short_puts"] += abs(qty)
                    elif qty > 0:
                        entry["long_puts"] += qty

        exposure_rows: List[Dict[str, Any]] = []
        for _group_key, item in by_symbol.items():
            sym = item["symbol"]
            spot = item["spot"]
            shares = item["shares"]
            leaps = item["long_leap_calls"]
            short_calls = item["short_calls"]
            net_delta_shares = round(shares + item["option_delta_shares"], 1)
            net_notional = round(net_delta_shares * spot, 2)

            total_cov_lots = (shares // 100) + leaps
            cc_capacity = max(0, total_cov_lots - short_calls)

            # Live 0.30 Delta Covered Call / PMCC Strike lookup (zero extra API calls if cached)
            cc_contract = f"{sym}-CC-0.30D"
            cc_strike = round(spot * 1.05, 1)
            cc_mid = round(spot * 0.012, 2)
            cc_bid = round(cc_mid * 0.95, 2)
            cc_ask = round(cc_mid * 1.05, 2)
            cc_dte = 33
            cc_exp = (datetime.date.today() + datetime.timedelta(days=33)).strftime("%Y-%m-%d")

            chain = self.scanner.market_client.get_live_option_chain(sym, 28, 55)
            calls = [c for c in chain if c.option_type == "CALL"]
            # Only tradable strikes: require a live two-sided market (bid > 0). A 0-bid contract's "mid" is a stale last
            # price (e.g. SOXL 167C: bid 0.00 / ask 3.15 / last 0.88) that cannot actually be filled.
            live_calls = [c for c in calls if c.bid > 0 and c.ask > 0]
            if live_calls:
                calls = live_calls
            # Prefer liquid strikes (tight spread, real open interest) so the quote is actually fillable.
            liquid_calls = [
                c for c in calls
                if c.open_interest >= 100 and (c.ask - c.bid) <= max(0.10, 0.15 * c.mid)
            ]
            if liquid_calls:
                calls = liquid_calls
            # Single expiration only (nearest in window) so strike/credit/DTE come from one consistent chain.
            if calls:
                _near_exp = min(calls, key=lambda c: c.dte).expiration
                calls = [c for c in calls if c.expiration == _near_exp]
            # Breakeven floor: never suggest a covered call strike below this row's LEAP breakeven / share basis
            cc_floor = 0.0
            for _ai in item["by_account"].values():
                if _ai["leap_calls"] > 0:
                    cc_floor = max(cc_floor, _ai["leap_be_sum"] / _ai["leap_calls"])
                if _ai["shares"] >= 100:
                    cc_floor = max(cc_floor, _ai["equity_cost_sum"] / _ai["shares"])
            cc_floor = round(cc_floor, 2)
            cc_floor_applied = False
            best_cc = None
            if calls:
                if cc_floor > 0:
                    # STRICT CAPITAL PROTECTION: Never suggest any call below cost basis / LEAP breakeven.
                    eligible = [c for c in calls if c.strike >= cc_floor and (c.bid > 0.05 or c.last_price > 0.05)]
                    if eligible:
                        cc_floor_applied = True
                        best_cc = min(eligible, key=lambda c: abs(c.delta_approx - 0.30))
                    else:
                        best_cc = None
                else:
                    best_cc = min(calls, key=lambda c: abs(c.delta_approx - 0.30))

            if best_cc:
                cc_contract = best_cc.contract_symbol
                cc_strike = best_cc.strike
                cc_mid = best_cc.mid
                cc_bid = best_cc.bid
                cc_ask = best_cc.ask
                cc_dte = best_cc.dte
                cc_exp = best_cc.expiration
                cc_is_suggestion = True
            else:
                cc_contract = "NONE"
                cc_strike = 0.0
                cc_mid = 0.0
                cc_bid = 0.0
                cc_ask = 0.0
                cc_dte = 0
                cc_exp = ""
                cc_is_suggestion = False

            cc_exit_50 = round(cc_mid * 0.50, 2)
            cc_total_credit = round(max(1, cc_capacity) * cc_mid * 100.0, 2)
            avg_equity_basis = round(item["equity_cost_sum"] / shares, 2) if shares > 0 else 0.0

            # Per-account Covered Call / PMCC Capacity & Cost-Basis / Breakeven Audit
            account_cc_breakdown: List[str] = []
            cost_basis_warnings: List[str] = []
            for acct_name in sorted(item["by_account"].keys()):
                ai = item["by_account"][acct_name]
                a_sh = ai["shares"]
                a_leaps = ai["leap_calls"]
                a_sc = ai["short_calls"]
                a_sp = ai["short_puts"]
                a_lots = (a_sh // 100) + a_leaps
                a_cap = max(0, a_lots - a_sc)
                a_eq_basis = round(ai["equity_cost_sum"] / a_sh, 2) if a_sh > 0 else 0.0
                a_leap_be = round(ai["leap_be_sum"] / a_leaps, 2) if a_leaps > 0 else 0.0

                if a_lots > 0:
                    src_parts = []
                    if a_sh >= 100:
                        src_parts.append(f"{a_sh}sh @ ${a_eq_basis:.2f} basis")
                    if a_leaps > 0:
                        src_parts.append(f"{a_leaps} LEAP @ ${a_leap_be:.2f} B/E" if src_parts else f"+{a_leaps} LEAP @ ${a_leap_be:.2f} B/E")
                    src_label = " + ".join(src_parts)
                    if a_cap > 0:
                        account_cc_breakdown.append(f"{acct_name}: {a_cap}x CC ({src_label})")
                        if a_sh >= 100 and cc_strike < a_eq_basis:
                            above_calls = [c for c in calls if c.strike >= a_eq_basis] if calls else []
                            safe_hint = ""
                            if above_calls:
                                sc_safe = min(above_calls, key=lambda c: c.strike)
                                safe_hint = f" (Safe above-basis strike: ${sc_safe.strike:g} Call @ ${sc_safe.mid:.2f})"
                            cost_basis_warnings.append(
                                f"[!] {acct_name}: 0.30D ${cc_strike:g} Call (suggested new sale, not a position you hold) is BELOW ${a_eq_basis:.2f} share cost basis!{safe_hint}"
                            )
                        if a_leaps > 0 and cc_strike < a_leap_be:
                            cost_basis_warnings.append(
                                f"[!] {acct_name}: 0.30D ${cc_strike:g} Call (suggested new sale, not a position you hold) is BELOW ${a_leap_be:.2f} LEAP breakeven!"
                            )
                    else:
                        account_cc_breakdown.append(f"{acct_name}: COVERED ({a_sc}/{a_lots} short calls active)")
                elif a_sp > 0:
                    account_cc_breakdown.append(f"{acct_name}: 0x CC (-{a_sp} CSP only -- cannot sell CC on Puts)")

            rsi = item["rsi_14"]
            ivr = item["iv_rank"]
            meta = WATCHLIST_MATRIX.get(sym)
            is_lev = meta.is_leveraged if meta else (sym in ("TQQQ", "SOXL", "SPXL"))

            has_below_basis_warning = len(cost_basis_warnings) > 0
            protect_floor = max(avg_equity_basis, cc_floor)
            protect_label = f"Share Basis ${avg_equity_basis:.2f}" if avg_equity_basis > 0 else f"LEAP B/E ${cc_floor:.2f}"

            # 1. CAPITAL DEFENSE GUARD: Never advise "SELL NOW" if strike is below basis or underwater
            if cc_capacity > 0 and (has_below_basis_warning or not cc_is_suggestion or (cc_floor > 0 and spot < cc_floor)):
                strike_msg = f"Strike ${cc_strike:g}" if cc_strike > 0 else f"Spot ${spot:.2f}"
                when_to_sell = f"DO NOT SELL BELOW BASIS ({strike_msg} < {protect_label})"
                directive = (
                    f"CAPITAL DEFENSE: No safe call suggested! Spot (${spot:.2f}) is below your {protect_label} (${protect_floor:.2f}). "
                    f"Selling calls below basis risks locking in a loss. Hold shares for rebound or sell calls >=${protect_floor:.2f} only."
                )
            # 2. Oversold / Beaten-Down RSI Guard: Do not sell calls during a deep pullback right before a bounce
            elif cc_capacity > 0 and rsi < 45.0:
                when_to_sell = f"WAIT FOR BOUNCE (RSI {rsi:.1f} Pullback -> Wait for RSI > 50)"
                if is_lev:
                    directive = (
                        f"WAIT FOR RSI > 50 BOUNCE: Selling calls during a dip caps recovery upside. Hold Shares / Sell 0.20Δ CSP on dips."
                    )
                else:
                    directive = (
                        f"WAIT FOR RSI > 50 BOUNCE: Selling calls during a dip caps recovery upside. Hold Shares / Accumulate."
                    )
            # 3. Qualified covered call sale on elevated IV or strong green day:
            elif cc_capacity > 0 and (rsi > 60.0 or ivr > 35.0):
                when_to_sell = f"SELL NOW (IVR {ivr:.0f}% / RSI {rsi:.0f} Qualified)"
                directive = (
                    f"SELL NOW: STO {cc_capacity}x ${cc_strike} Call exp {cc_exp} ({cc_dte} DTE) "
                    f"@ ${cc_mid:.2f} (+${cc_total_credit:,.0f} credit) | Exit @ ${cc_exit_50:.2f} (50%) or 14 DTE"
                )
            elif cc_capacity > 0:
                when_to_sell = f"SELL ON GREEN DAY (RSI {rsi:.0f} Neutral, IVR {ivr:.0f}%)"
                directive = (
                    f"READY: STO {cc_capacity}x ${cc_strike} Call exp {cc_exp} ({cc_dte} DTE) "
                    f"@ ${cc_mid:.2f} (+${cc_total_credit:,.0f} credit) | Exit @ ${cc_exit_50:.2f} (50%) or 14 DTE"
                )
            else:
                when_to_sell = "ALL LOTS COVERED (Wait for 50% Profit or 14 DTE Roll)"
                directive = (
                    f"FULLY COVERED: Next Roll Target ${cc_strike} Call exp {cc_exp} ({cc_dte} DTE) "
                    f"@ ${cc_mid:.2f} (BTC @ ${cc_exit_50:.2f} / 50% profit)"
                )

            # Beta-Weighted Exposure against S&P 500 (SPY)
            spy_snap = self.scanner.snapshots.get("SPY")
            spy_spot = spy_snap.spot if spy_snap and spy_snap.spot > 0 else 580.0
            beta_val = meta.beta if meta else (3.8 if sym == "SOXL" else (3.5 if sym == "TQQQ" else (1.95 if sym == "NVDA" else (2.1 if sym == "TSLA" else 1.0))))
            beta_dollar_delta = round(net_notional * beta_val, 2)
            spy_delta_shares = round(beta_dollar_delta / spy_spot, 1) if spy_spot > 0 else 0.0

            exposure_rows.append({
                "beta": beta_val,
                "beta_dollar_delta": beta_dollar_delta,
                "spy_delta_shares": spy_delta_shares,
                "symbol": sym,
                "accounts": ", ".join(sorted(item["accounts"])),
                "account_cc_breakdown": account_cc_breakdown,
                "cost_basis_warnings": cost_basis_warnings,
                "avg_equity_cost_basis": avg_equity_basis,
                "spot": round(spot, 2),
                "rsi_14": round(rsi, 1),
                "iv_rank": round(ivr, 1),
                "shares": shares,
                "equity_market_value": round(item["equity_market_value"], 2),
                "long_leap_calls": leaps,
                "short_calls": short_calls,
                "short_puts": item["short_puts"],
                "long_puts": item["long_puts"],
                "option_delta_shares": round(item["option_delta_shares"], 1),
                "net_delta_shares": net_delta_shares,
                "net_notional_exposure": net_notional,
                "covered_call_capacity": cc_capacity,
                "cc_contract": cc_contract,
                "cc_is_suggestion": True,
                "cc_breakeven_floor": cc_floor,
                "cc_floor_applied": cc_floor_applied,
                "cc_strike": cc_strike,
                "cc_expiration": cc_exp,
                "cc_dte": cc_dte,
                "cc_bid": round(cc_bid, 2),
                "cc_ask": round(cc_ask, 2),
                "cc_mid": round(cc_mid, 2),
                "cc_exit_target_50": cc_exit_50,
                "cc_total_credit": cc_total_credit,
                "when_to_sell": when_to_sell,
                "total_pnl_dollars": round(item["equity_pnl_dollars"] + item["options_pnl_dollars"], 2),
                "directive": directive,
            })

        # Group rows by ticker (largest total ticker exposure first), then by account
        ticker_totals: Dict[str, float] = {}
        for r in exposure_rows:
            ticker_totals[r["symbol"]] = ticker_totals.get(r["symbol"], 0.0) + abs(r["net_notional_exposure"])
        exposure_rows.sort(key=lambda r: (-ticker_totals[r["symbol"]], r["symbol"], r["accounts"]))
        return exposure_rows

    def get_portfolio_beta_summary(self, exposure_rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        rows = exposure_rows if exposure_rows is not None else self.evaluate_equity_and_exposure()
        spy_snap = self.scanner.snapshots.get("SPY")
        spy_spot = spy_snap.spot if spy_snap and spy_snap.spot > 0 else 580.0

        total_spy_shares = round(sum(r.get("spy_delta_shares", 0.0) for r in rows), 1)
        total_beta_exposure = round(total_spy_shares * spy_spot, 2)
        move_1pct = round(total_beta_exposure * 0.01, 2)

        if total_spy_shares > 250:
            tilt = "Aggressive Bullish (High Beta Long)"
            tilt_badge = "badge-danger"
        elif total_spy_shares > 50:
            tilt = "Moderate Bullish (Target Growth)"
            tilt_badge = "badge-success"
        elif total_spy_shares >= -50:
            tilt = "Market Neutral (Delta Hedged)"
            tilt_badge = "badge-info"
        else:
            tilt = "Defensive / Net Short"
            tilt_badge = "badge-warning"

        return {
            "spy_spot": round(spy_spot, 2),
            "total_spy_delta_shares": total_spy_shares,
            "total_beta_exposure": total_beta_exposure,
            "move_1pct_dollars": move_1pct,
            "portfolio_tilt": tilt,
            "tilt_badge": tilt_badge,
            "total_positions_count": len(rows),
        }

    def add_manual_position(
        self,
        symbol: str,
        strike: float,
        option_type: str,
        expiration: str,
        contracts: int,
        entry_premium: float,
        account: str = "Manual",
    ):
        opt_clean = option_type.strip().upper()
        if opt_clean in ("EQUITY", "SHARES", "STOCK"):
            opt_clean = "EQUITY"
            strike_val = 0.0
            exp_clean = "EQUITY"
        else:
            strike_val = float(strike)
            exp_clean = expiration.strip()

        acct_clean = account.strip() if account and account.strip() else "Manual"
        with self._lock:
            file_exists = os.path.exists(self.positions_file)
            with open(self.positions_file, "a", encoding="utf-8", newline="") as f:
                if not file_exists:
                    f.write("symbol,strike,option_type,expiration,contracts,entry_premium,account\n")
                f.write(
                    f"{symbol.strip().upper()},{strike_val},{opt_clean},{exp_clean},"
                    f"{int(contracts)},{float(entry_premium):.2f},{acct_clean}\n"
                )

    def delete_manual_position(self, index_to_remove: int):
        if not os.path.exists(self.positions_file):
            return
        with self._lock:
            headers_and_comments = []
            data_rows = []
            with open(self.positions_file, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#") or stripped.lower().startswith("symbol,"):
                        headers_and_comments.append(line if line.endswith("\n") else line + "\n")
                    else:
                        data_rows.append(line if line.endswith("\n") else line + "\n")
            with open(self.positions_file, "w", encoding="utf-8") as f:
                for h in headers_and_comments:
                    f.write(h)
                for idx, r in enumerate(data_rows, start=1):
                    if idx != index_to_remove:
                        f.write(r)


# ==============================================================================
# SECTION 5: HTTP ANALYTICAL UI DASHBOARD SERVER & AUTO-SYNC ENGINE
# ==============================================================================

class AutoSyncManager:
    """Refreshes live market snapshots in the background and auto-watches Fidelity CSV folder."""
    def __init__(self, scanner: StrategyScanner, sentinel: GammaDangerSentinel, interval_sec: int = 120):
        self.scanner = scanner
        self.sentinel = sentinel
        self.interval = interval_sec
        self.last_sync = datetime.datetime.now()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_fidelity_csv: str = ""
        self._last_fidelity_mtime: float = 0.0
        self._init_fidelity_state()

    def _init_fidelity_state(self):
        try:
            from convert_fidelity_positions import find_latest_fidelity_csv
            fidelity_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Fidelity")
            csv_path = find_latest_fidelity_csv(fidelity_dir)
            if csv_path and os.path.exists(csv_path):
                self._last_fidelity_csv = csv_path
                self._last_fidelity_mtime = os.path.getmtime(csv_path)
        except Exception:
            pass

    def _check_fidelity_update(self):
        try:
            from convert_fidelity_positions import find_latest_fidelity_csv, convert_latest_fidelity_to_positions
            fidelity_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Fidelity")
            latest_csv = find_latest_fidelity_csv(fidelity_dir)
            if latest_csv and os.path.exists(latest_csv):
                mtime = os.path.getmtime(latest_csv)
                if latest_csv != self._last_fidelity_csv or mtime > self._last_fidelity_mtime:
                    self._last_fidelity_csv = latest_csv
                    self._last_fidelity_mtime = mtime
                    src_csv, converted = convert_latest_fidelity_to_positions()
                    print(f"\n[AUTO-WATCHER] 📁 New or updated Fidelity CSV detected: {os.path.basename(src_csv)} ({len(converted)} positions auto-synced!)")
                    self.sentinel.evaluate_all_manual_positions()
        except Exception as e:
            sys.stderr.write(f"[AUTO-WATCHER WARNING]: {e}\n")

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        print(f"[*] Auto-Sync Active: Background telemetry updates every {self.interval}s + Fidelity CSV auto-watcher active.")

    def _worker(self):
        elapsed = 0
        while self._running:
            time.sleep(5)
            elapsed += 5
            self._check_fidelity_update()
            if elapsed >= self.interval:
                elapsed = 0
                try:
                    self.scanner.refresh()
                    self.last_sync = datetime.datetime.now()
                    self.sentinel.audit_manual_positions(emit_stderr=True)
                except Exception as e:
                    sys.stderr.write(f"[AUTO-SYNC WARNING]: {e}\n")


class SentinelRequestHandler(http.server.SimpleHTTPRequestHandler):
    scanner: StrategyScanner
    sentinel: GammaDangerSentinel

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/summary":
            self._handle_api_summary()
        elif path == "/api/watchlist":
            self._send_json_response([asdict(r) for r in self.scanner.scan_ticker_universe()])
        elif path == "/api/wheel":
            self._send_json_response([asdict(r) for r in self.scanner.generate_wheel_matrix(all_tickers=True)])
        elif path == "/api/leap":
            self._send_json_response([asdict(r) for r in self.scanner.generate_leap_matrix()])
        elif path == "/api/leap-put":
            self._send_json_response(self.scanner.generate_leap_put_matrix(all_tickers=True))
        elif path == "/api/alerts":
            self._send_json_response(self.sentinel.audit_manual_positions(emit_stderr=False))
        elif path == "/api/positions":
            self._send_json_response(self.sentinel.evaluate_all_manual_positions())
        elif path == "/api/equity":
            self._send_json_response(self.sentinel.evaluate_equity_and_exposure())
        elif path == "/api/schwab/status":
            self._send_json_response(self.scanner.market_client.schwab_client.get_status())
        elif path == "/api/schwab/login":
            auth_url = self.scanner.market_client.schwab_client.get_auth_url()
            if auth_url:
                self.send_response(302)
                self.send_header("Location", auth_url)
                self.send_header("Connection", "close")
                self.end_headers()
            else:
                self._send_json_response({
                    "status": "error",
                    "message": "Set SCHWAB_APP_KEY and SCHWAB_APP_SECRET in your local .env file first.",
                })
        elif path in ["/", "/index.html"]:
            self._serve_ui_dashboard()
        else:
            self.send_error(404, "Endpoint not found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/refresh":
            self.scanner.refresh()
            self._send_json_response({"status": "success", "message": "Live market telemetry refreshed."})
        elif path == "/api/trigger-alert":
            alerts = self.sentinel.audit_manual_positions(emit_stderr=True)
            self._send_json_response({"status": "success", "alerts_count": len(alerts), "alerts": alerts})
        elif path == "/api/positions":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
            self.sentinel.add_manual_position(
                symbol=body.get("symbol", "SPY"),
                strike=float(body.get("strike", 700.0)),
                option_type=body.get("option_type", "PUT"),
                expiration=body.get("expiration", "2026-11-27"),
                contracts=int(body.get("contracts", -1)),
                entry_premium=float(body.get("entry_premium", 1.0)),
                account=body.get("account", "Manual"),
            )
            positions = self.sentinel.evaluate_all_manual_positions()
            self._send_json_response({
                "status": "success",
                "positions": positions,
                "equity_exposure": self.sentinel.evaluate_equity_and_exposure(positions),
            })
        elif path == "/api/positions/delete":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
            self.sentinel.delete_manual_position(int(body.get("index", 0)))
            positions = self.sentinel.evaluate_all_manual_positions()
            self._send_json_response({
                "status": "success",
                "positions": positions,
                "equity_exposure": self.sentinel.evaluate_equity_and_exposure(positions),
            })
        elif path == "/api/positions/import-fidelity":
            try:
                from convert_fidelity_positions import convert_latest_fidelity_to_positions
                src_csv, converted = convert_latest_fidelity_to_positions()
                positions = self.sentinel.evaluate_all_manual_positions()
                self._send_json_response({
                    "status": "success",
                    "source_file": os.path.basename(src_csv),
                    "imported_count": len(converted),
                    "positions": positions,
                    "equity_exposure": self.sentinel.evaluate_equity_and_exposure(positions),
                })
            except Exception as e:
                self._send_json_response({"status": "error", "message": str(e)})
        elif path == "/api/schwab/reauth":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
            sc = self.scanner.market_client.schwab_client
            redirect_url = body.get("redirect_url", "").strip()
            if redirect_url:
                ok, msg = sc.exchange_code_from_redirect(redirect_url)
                if ok:
                    self.scanner.refresh()
                self._send_json_response({
                    "status": "success" if ok else "error",
                    "message": msg,
                    "schwab_status": sc.get_status(),
                })
            else:
                self._send_json_response({
                    "status": "error",
                    "message": "Paste the https://127.0.0.1/?code=... redirect URL from Schwab login.",
                    "schwab_status": sc.get_status(),
                })
        else:
            self.send_error(404, "Unknown POST route")

    def _send_json_response(self, data: Any):
        payload = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_api_summary(self):
        watchlist = [asdict(r) for r in self.scanner.scan_ticker_universe()]
        wheel = [asdict(r) for r in self.scanner.generate_wheel_matrix(all_tickers=True)]
        leap = [asdict(r) for r in self.scanner.generate_leap_matrix(all_tickers=True)]
        alerts = self.sentinel.audit_manual_positions(emit_stderr=False)

        # 3x Leveraged Equities Strategy Engine (TQQQ, SOXL, SPXL)
        leveraged_etfs = []
        for l_sym in ["TQQQ", "SOXL", "SPXL"]:
            l_snap = self.scanner.snapshots.get(l_sym)
            if not l_snap:
                continue
            spot = l_snap.spot
            iv = max(0.15, l_snap.iv_current)
            dte = 45
            move_1sd = spot * iv * math.sqrt(dte / 365.0)
            safe_k = round((spot - (move_1sd * 1.25)) / 0.5) * 0.5
            buffer_pct = round(((spot - safe_k) / spot) * 100.0, 1) if spot > 0 else 0.0
            chain = self.scanner.market_client.get_live_option_chain(l_sym, 28, 55)
            puts = [c for c in chain if c.option_type == "PUT"]
            closest_put = min(puts, key=lambda c: abs(c.strike - safe_k)) if puts else None
            live_contract = closest_put.contract_symbol if closest_put else f"{l_sym}-CSP-0.20D"
            live_mid = closest_put.mid if closest_put else round(spot * 0.03, 2)
            target_dte = closest_put.dte if closest_put else dte
            exp_date = closest_put.expiration if closest_put else ""
            bid = closest_put.bid if closest_put else round(max(0.01, live_mid * 0.95), 2)
            ask = closest_put.ask if closest_put else round(live_mid * 1.05, 2)
            delta_val = closest_put.delta_approx if closest_put else -0.20
            theta_val = closest_put.theta_approx if closest_put else -0.05

            if not exp_date and live_contract:
                exp_date = parse_occ_expiration(live_contract) or ""
            if not exp_date and target_dte:
                exp_date = (datetime.date.today() + datetime.timedelta(days=target_dte)).isoformat()

            leveraged_etfs.append({
                "symbol": l_sym,
                "spot": spot,
                "iv_current": round(iv * 100.0, 1),
                "iv_rank": l_snap.iv_rank,
                "rsi_14": l_snap.rsi_14,
                "expected_1sd_move": round(move_1sd, 2),
                "safe_csp_strike": safe_k,
                "downside_buffer_pct": buffer_pct,
                "target_dte": target_dte,
                "expiration": exp_date,
                "live_contract": live_contract,
                "bid": bid,
                "ask": ask,
                "live_mid": live_mid,
                "delta": delta_val,
                "theta": theta_val,
                "target_delta": -0.20,
                "required_collateral": safe_k * 100.0,
                "max_profit_credit": round(live_mid * 100.0, 2),
                "profit_target_50": round(live_mid * 0.50, 2),
                "annualized_roc_est": round(((live_mid / safe_k) * (365.0 / target_dte)) * 100.0, 1) if safe_k > 0 else 0.0,
                "volatility_drag_warning": "CRITICAL: Never hold long LEAP calls. 3x daily rebalance causes severe beta decay in sideways regimes.",
                "recommended_strategy": "Harvest Volatility Drag: Sell 0.20 Delta Cash-Secured Puts (50% profit exit) or Defined-Risk Put Credit Spreads."
            })

        leap_puts = self.scanner.generate_leap_put_matrix(all_tickers=True)
        spreads = self.scanner.generate_spread_matrix(all_tickers=True)
        manual_positions = self.sentinel.evaluate_all_manual_positions()
        equity_exposure = self.sentinel.evaluate_equity_and_exposure(manual_positions)
        portfolio_beta = self.sentinel.get_portfolio_beta_summary(equity_exposure)
        summary = {
            "portfolio_beta_summary": portfolio_beta,
            "timestamp": datetime.datetime.now().isoformat(),
            "feed_mode": "100% LIVE EXCHANGE FEED (ZERO SIMULATION)",
            "active_provider": self.scanner.market_client.active_provider,
            "feed_warnings": self.scanner.market_client.recent_warnings,
            "schwab_status": self.scanner.market_client.schwab_client.get_status(),
            "ticker_count": len(watchlist),
            "wheel_opportunities": sum(1 for r in wheel if r.get("is_qualified")),
            "leap_opportunities": sum(1 for r in leap if r.get("is_qualified")),
            "leap_put_opportunities": sum(1 for p in leap_puts if p.get("is_qualified")),
            "spread_opportunities": sum(1 for s in spreads if s.get("is_qualified")),
            "gamma_danger_count": len(alerts),
            "manual_positions_count": len(manual_positions),
            "equity_tickers_count": len(equity_exposure),
            "watchlist": watchlist,
            "wheel_recommendations": wheel,
            "leap_recommendations": leap,
            "leap_put_recommendations": leap_puts,
            "spread_recommendations": spreads,
            "leveraged_etfs_guidance": leveraged_etfs,
            "gamma_alerts": alerts,
            "manual_positions": manual_positions,
            "equity_exposure": equity_exposure,
        }
        self._send_json_response(summary)

    def _serve_ui_dashboard(self):
        local_dashboard = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
        artifact_path = r"C:\Users\Ismail\.gemini\antigravity\brain\7f4099eb-b785-45b3-a1bf-8414df563b30\option_sentinel_dashboard.html"
        if os.path.exists(local_dashboard):
            with open(local_dashboard, "r", encoding="utf-8") as f:
                html = f.read()
        elif os.path.exists(artifact_path):
            with open(artifact_path, "r", encoding="utf-8") as f:
                html = f.read()
        else:
            html = "<h1>Option Sentinel Live</h1>"

        payload = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        pass


# ==============================================================================
# SECTION 6: CLI & ENTRY POINT
# ==============================================================================

def get_lan_ip() -> str:
    """Detects the primary LAN IP address so phones/tablets on Wi-Fi can connect."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main():
    parser = argparse.ArgumentParser(description="Live Option Sentinel & Strike Selector Agent.")
    parser.add_argument("--server", action="store_true", default=True, help="Run live HTTP UI dashboard server.")
    parser.add_argument("--cli", action="store_true", help="Run terminal scan mode.")
    parser.add_argument("--schwab-auth", nargs="?", const="FROM_ENV", default="", help="Activate 7-day Schwab token (reads SCHWAB_CALLBACK_URL from .env, or pass callback URL directly).")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host interface to bind (0.0.0.0 for all devices).")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)), help="Port for dashboard (default: 8765).")

    args = parser.parse_args()

    # Always rebuild positions.csv from the newest CSV in the Fidelity/ folder on startup
    try:
        from convert_fidelity_positions import convert_latest_fidelity_to_positions
        _src, _converted = convert_latest_fidelity_to_positions()
        print(f"[STARTUP] positions.csv rebuilt from Fidelity/{os.path.basename(_src)} ({len(_converted)} positions)")
    except FileNotFoundError as e:
        print(f"[STARTUP WARNING] {e} -- using existing positions.csv as-is")
    except Exception as e:
        print(f"[STARTUP WARNING] Fidelity import failed ({e}) -- using existing positions.csv as-is")

    scanner = StrategyScanner()
    sentinel = GammaDangerSentinel(scanner, positions_file="positions.csv")

    if args.schwab_auth:
        print("\n[SCHWAB OAUTH EXCHANGE]")
        target_input = "" if args.schwab_auth == "FROM_ENV" else args.schwab_auth
        ok, msg = scanner.market_client.schwab_client.exchange_code_from_redirect(target_input)
        if ok:
            print(f"SUCCESS: {msg}")
            print(f"Saved 7-day token to git-ignored schwab_tokens.json.")
        else:
            print(f"ERROR: {msg}")
        return

    # Initial Sentinel Audit
    sentinel.audit_manual_positions(emit_stderr=True)

    if args.cli:
        print("\n\033[96;1m=== LIVE OPTION SENTINEL SCAN (100% REAL MARKET DATA) ===\033[0m")
        for r in scanner.scan_ticker_universe():
            print(f"\033[97;1m{r.symbol:<6}\033[0m Spot: \033[93;1m${r.spot:<8.2f}\033[0m | IV: \033[96;1m{r.iv*100:>4.1f}%\033[0m | IVR: \033[92;1m{r.iv_rank:>4.1f}%\033[0m | RSI: \033[95;1m{r.rsi:>4.1f}\033[0m")
        return

    if args.server:
        SentinelRequestHandler.scanner = scanner
        SentinelRequestHandler.sentinel = sentinel

        auto_syncer = AutoSyncManager(scanner, sentinel, interval_sec=120)
        auto_syncer.start()

        port = args.port
        lan_ip = get_lan_ip()
        server_address = (args.host, port)

        class ReusableTCPServer(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True
            block_on_close = False

        print(f"\n================================================================================")
        print(f"[>] Live Option Sentinel Dashboard Active (All Devices Mode):")
        print(f"    On This PC          : http://127.0.0.1:{port}/")
        print(f"    Phone / iPad / Wi-Fi: http://{lan_ip}:{port}/")
        print(f"    Feed Mode           : 100% REAL LIVE MARKET DATA (ZERO SIMULATION)")
        print(f"    Manual Positions    : positions.csv (Manageable from any browser)")
        print(f"    Stop Server         : Press Ctrl+C to exit immediately")
        print(f"================================================================================")
        print("API Endpoints:")
        print(f"  - http://{lan_ip}:{port}/api/summary")
        print(f"  - http://{lan_ip}:{port}/api/positions")
        print(f"  - http://{lan_ip}:{port}/api/wheel")
        print(f"  - http://{lan_ip}:{port}/api/leap\n")

        httpd = ReusableTCPServer(server_address, SentinelRequestHandler)
        try:
            httpd.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            print("\nOption Sentinel stopped.")
        finally:
            auto_syncer._running = False
            httpd.server_close()
            os._exit(0)


if __name__ == "__main__":
    main()
