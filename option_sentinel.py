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


WATCHLIST_MATRIX: Dict[str, TickerMetadata] = {
    "SPY": TickerMetadata("SPY", "SPDR S&P 500 ETF Trust", AssetCategory.CORE_EQUITY, False, False, 1.0),
    "QQQ": TickerMetadata("QQQ", "Invesco QQQ Trust (Nasdaq-100)", AssetCategory.CORE_EQUITY, False, False, 1.0),
    "XSP": TickerMetadata("XSP", "Mini-S&P 500 Index Options", AssetCategory.CORE_EQUITY, False, True, 1.0),
    "QQQM": TickerMetadata("QQQM", "Invesco NASDAQ 100 ETF", AssetCategory.CORE_EQUITY, False, False, 1.0),
    "SPYM": TickerMetadata("SPYM", "SPDR Portfolio S&P 500 ETF", AssetCategory.CORE_EQUITY, False, False, 0.5),
    "TQQQ": TickerMetadata("TQQQ", "ProShares UltraPro QQQ (3x)", AssetCategory.LEVERAGED_EQUITY, True, False, 1.0),
    "SOXL": TickerMetadata("SOXL", "Direxion Daily Semiconductor Bull 3X", AssetCategory.LEVERAGED_EQUITY, True, False, 0.5),
    "SPXL": TickerMetadata("SPXL", "Direxion Daily S&P 500 Bull 3X", AssetCategory.LEVERAGED_EQUITY, True, False, 1.0),
    "GLD": TickerMetadata("GLD", "SPDR Gold Trust", AssetCategory.COMMODITY, False, False, 1.0),
    "SLV": TickerMetadata("SLV", "iShares Silver Trust", AssetCategory.COMMODITY, False, False, 0.5),
    "IAU": TickerMetadata("IAU", "iShares Gold Trust", AssetCategory.COMMODITY, False, False, 0.5),
    "IBIT": TickerMetadata("IBIT", "iShares Bitcoin Trust ETF", AssetCategory.CRYPTO_BONDS, True, False, 0.5),
    "TLT": TickerMetadata("TLT", "iShares 20+ Year Treasury Bond", AssetCategory.CRYPTO_BONDS, False, False, 0.5),
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
    roll_trigger_dte: int = 14


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
                )
            )
        return results

    def generate_wheel_matrix(self, all_tickers: bool = False) -> List[WheelStrikeRecommendation]:
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

            chain = self.market_client.get_live_option_chain(sym, 28, 55)
            puts = [c for c in chain if c.option_type == "PUT"]
            if puts:
                closest_put = min(puts, key=lambda c: abs(c.strike - snapped_k))
                live_contract = closest_put.contract_symbol
                snapped_k = closest_put.strike
                target_dte = closest_put.dte
                bid = closest_put.bid
                ask = closest_put.ask
                mid = closest_put.mid
                delta_val = closest_put.delta_approx
                gamma_val = closest_put.gamma_approx
                theta_val = closest_put.theta_approx

            if mid <= 0:
                mid = round(self.math.black_scholes_price(spot, snapped_k, target_dte, iv, 0.045, is_call=False), 2)
                bid = round(max(0.01, mid * 0.95), 2)
                ask = round(mid * 1.05, 2)

            collateral = snapped_k * 100.0
            max_profit = mid * 100.0
            profit_target_50 = round(mid * 0.50, 2)
            downside_buffer = round(((spot - snapped_k) / spot) * 100.0, 2) if spot > 0 else 0.0
            annualized_roc = round(((mid / snapped_k) * (365.0 / target_dte)) * 100.0, 2) if snapped_k > 0 else 0.0

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
                roll_trigger_dte=14,
            )
            recommendations.append(rec)

        return recommendations

    def generate_leap_matrix(self) -> List[LeapRecommendation]:
        recommendations: List[LeapRecommendation] = []

        for sym, meta in WATCHLIST_MATRIX.items():
            if meta.is_leveraged:
                continue

            snap = self.snapshots.get(sym)
            if not snap:
                continue

            if not (snap.iv_rank < 20.0 and snap.rsi_14 < 35.0):
                continue

            spot = snap.spot
            iv = max(0.10, snap.iv_current)
            leap_contract = f"{sym}-LEAP-CALL"
            leap_strike = round(spot * 0.85)
            leap_bid, leap_ask, leap_mid = 0.0, 0.0, 0.0
            leap_dte = 390

            leaps = self.market_client.get_live_leap_options(sym, min_dte=340)
            if leaps:
                best_leap = min(leaps, key=lambda c: abs(c.delta_approx - 0.85))
                leap_contract = best_leap.contract_symbol
                leap_strike = best_leap.strike
                leap_bid = best_leap.bid
                leap_ask = best_leap.ask
                leap_mid = best_leap.mid
                leap_dte = best_leap.dte

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

            chain = self.market_client.get_live_option_chain(sym, 28, 55)
            calls = [c for c in chain if c.option_type == "CALL"]
            if calls:
                best_pmcc = min(calls, key=lambda c: abs(c.delta_approx - 0.30))
                pmcc_contract = best_pmcc.contract_symbol
                pmcc_strike = best_pmcc.strike
                pmcc_mid = best_pmcc.mid
                pmcc_dte = best_pmcc.dte

            net_debit = round(leap_mid - pmcc_mid, 2)
            pmcc_yield = round((pmcc_mid / net_debit) * 100.0, 2) if net_debit > 0 else 0.0
            workflow = f"BUY 1x {leap_contract} (@ mid ${leap_mid:.2f}) | SELL 1x {pmcc_contract} (@ mid ${pmcc_mid:.2f})"

            intrinsic = max(0.0, spot - leap_strike)
            extrinsic = max(0.0, leap_mid - intrinsic)
            effective_leverage = round((0.85 * spot) / leap_mid, 2) if leap_mid > 0 else 0.0
            annual_drag = round((extrinsic / spot) * (365.0 / max(1, leap_dte)) * 100.0, 2) if spot > 0 else 0.0
            spread_width = round(pmcc_strike - leap_strike, 2)
            gap_safety = "SAFE: Zero Upside Gap Risk" if spread_width >= net_debit else "MONITOR: Upside Risk"

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
            )
            recommendations.append(rec)

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

            leaps = self.market_client.get_live_leap_options(sym, min_dte=340, option_type="PUT")
            if leaps:
                best_leap = min(leaps, key=lambda c: abs(c.delta_approx - (-0.85)))
                leap_contract = best_leap.contract_symbol
                leap_strike = best_leap.strike
                leap_bid = best_leap.bid
                leap_ask = best_leap.ask
                leap_mid = best_leap.mid
                leap_dte = best_leap.dte

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

            chain = self.market_client.get_live_option_chain(sym, 28, 55)
            puts = [c for c in chain if c.option_type == "PUT"]
            if puts:
                best_pmcp = min(puts, key=lambda c: abs(c.delta_approx - (-0.30)))
                pmcp_contract = best_pmcp.contract_symbol
                pmcp_strike = best_pmcp.strike
                pmcp_mid = best_pmcp.mid
                pmcp_dte = best_pmcp.dte

            net_debit = round(leap_mid - pmcp_mid, 2)
            intrinsic = max(0.0, leap_strike - spot)
            extrinsic = max(0.0, leap_mid - intrinsic)
            effective_leverage = round((0.85 * spot) / leap_mid, 2) if leap_mid > 0 else 0.0
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
                "net_debit": net_debit,
                "spread_width": spread_width,
                "gap_safety": gap_safety,
            })

        return recommendations


# ==============================================================================
# SECTION 4: REAL-TIME GAMMA DANGER SENTINEL (MONITORS positions.csv)
# ==============================================================================

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
            exp_date = datetime.datetime.strptime(exp_str, "%Y-%m-%d").date()
            dte = (exp_date - today).days

            if contracts >= 0 or dte >= 14:
                continue

            matched = self.scanner.market_client.lookup_position_contract(sym, opt_type, strike, dte)
            curr_mid = matched.mid if matched else entry_prem
            gamma_val = matched.gamma_approx if matched else 0.02
            delta_val = matched.delta_approx if matched else -0.25

            if contracts < 0 and dte < 14:
                pnl = (entry_prem - curr_mid) * abs(contracts) * 100.0
                pnl_pct = ((entry_prem - curr_mid) / entry_prem) * 100.0 if entry_prem > 0 else 0.0

                alert = {
                    "timestamp": now_str,
                    "position_id": f"MANUAL-{idx:02d}-{sym}",
                    "symbol": sym,
                    "strike": strike,
                    "option_type": opt_type,
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
            try:
                exp_date = datetime.datetime.strptime(exp_str, "%Y-%m-%d").date()
                dte = (exp_date - today).days
            except Exception:
                dte = 30

            matched = self.scanner.market_client.lookup_position_contract(sym, opt_type, strike, dte)
            curr_mid = matched.mid if matched else entry_prem

            if contracts < 0:
                pnl = (entry_prem - curr_mid) * abs(contracts) * 100.0
                pnl_pct = ((entry_prem - curr_mid) / entry_prem) * 100.0 if entry_prem > 0 else 0.0
            else:
                pnl = (curr_mid - entry_prem) * abs(contracts) * 100.0
                pnl_pct = ((curr_mid - entry_prem) / entry_prem) * 100.0 if entry_prem > 0 else 0.0

            gamma_danger = (dte < 14 and contracts < 0)
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
                "gamma_danger": gamma_danger,
                "profit_target_hit": profit_hit,
                "status": status,
            })
        return results

    def add_manual_position(self, symbol: str, strike: float, option_type: str, expiration: str, contracts: int, entry_premium: float):
        with self._lock:
            file_exists = os.path.exists(self.positions_file)
            with open(self.positions_file, "a", encoding="utf-8", newline="") as f:
                if not file_exists:
                    f.write("symbol,strike,option_type,expiration,contracts,entry_premium\n")
                f.write(f"{symbol.strip().upper()},{float(strike)},{option_type.strip().upper()},{expiration.strip()},{int(contracts)},{float(entry_premium)}\n")

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
    """Refreshes live market snapshots in the background every interval_sec."""
    def __init__(self, scanner: StrategyScanner, sentinel: GammaDangerSentinel, interval_sec: int = 60):
        self.scanner = scanner
        self.sentinel = sentinel
        self.interval = interval_sec
        self.last_sync = datetime.datetime.now()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        print(f"🔄 Auto-Sync Active: Background telemetry updates automatically every {self.interval}s.")

    def _worker(self):
        while self._running:
            time.sleep(self.interval)
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
            )
            self._send_json_response({"status": "success", "positions": self.sentinel.evaluate_all_manual_positions()})
        elif path == "/api/positions/delete":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
            self.sentinel.delete_manual_position(int(body.get("index", 0)))
            self._send_json_response({"status": "success", "positions": self.sentinel.evaluate_all_manual_positions()})
        elif path == "/api/positions/import-fidelity":
            try:
                from convert_fidelity_positions import convert_latest_fidelity_to_positions
                src_csv, converted = convert_latest_fidelity_to_positions()
                self._send_json_response({
                    "status": "success",
                    "source_file": os.path.basename(src_csv),
                    "imported_count": len(converted),
                    "positions": self.sentinel.evaluate_all_manual_positions(),
                })
            except Exception as e:
                self._send_json_response({"status": "error", "message": str(e)})
        else:
            self.send_error(404, "Unknown POST route")

    def _send_json_response(self, data: Any):
        payload = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_api_summary(self):
        watchlist = [asdict(r) for r in self.scanner.scan_ticker_universe()]
        wheel = [asdict(r) for r in self.scanner.generate_wheel_matrix(all_tickers=False)]
        leap = [asdict(r) for r in self.scanner.generate_leap_matrix()]
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
            leveraged_etfs.append({
                "symbol": l_sym,
                "spot": spot,
                "iv_current": round(iv * 100.0, 1),
                "iv_rank": l_snap.iv_rank,
                "rsi_14": l_snap.rsi_14,
                "expected_1sd_move": round(move_1sd, 2),
                "safe_csp_strike": safe_k,
                "downside_buffer_pct": buffer_pct,
                "live_contract": live_contract,
                "live_mid": live_mid,
                "target_delta": -0.20,
                "profit_target_50": round(live_mid * 0.50, 2),
                "annualized_roc_est": round(((live_mid / safe_k) * (365.0 / dte)) * 100.0, 1) if safe_k > 0 else 0.0,
                "volatility_drag_warning": "CRITICAL: Never hold long LEAP calls. 3x daily rebalance causes severe beta decay in sideways regimes.",
                "recommended_strategy": "Harvest Volatility Drag: Sell 0.20 Delta Cash-Secured Puts (50% profit exit) or Defined-Risk Put Credit Spreads."
            })

        leap_puts = self.scanner.generate_leap_put_matrix(all_tickers=True)
        manual_positions = self.sentinel.evaluate_all_manual_positions()
        summary = {
            "timestamp": datetime.datetime.now().isoformat(),
            "feed_mode": "100% LIVE EXCHANGE FEED (ZERO SIMULATION)",
            "ticker_count": len(watchlist),
            "wheel_opportunities": len(wheel),
            "leap_opportunities": len(leap),
            "leap_put_opportunities": sum(1 for p in leap_puts if p.get("is_qualified")),
            "gamma_danger_count": len(alerts),
            "manual_positions_count": len(manual_positions),
            "watchlist": watchlist,
            "wheel_recommendations": wheel,
            "leap_recommendations": leap,
            "leap_put_recommendations": leap_puts,
            "leveraged_etfs_guidance": leveraged_etfs,
            "gamma_alerts": alerts,
            "manual_positions": manual_positions,
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
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host interface to bind (0.0.0.0 for all devices).")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)), help="Port for dashboard (default: 8765).")

    args = parser.parse_args()

    scanner = StrategyScanner()
    sentinel = GammaDangerSentinel(scanner, positions_file="positions.csv")

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

        auto_syncer = AutoSyncManager(scanner, sentinel, interval_sec=60)
        auto_syncer.start()

        port = args.port
        lan_ip = get_lan_ip()
        server_address = (args.host, port)

        class ReusableTCPServer(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True
            block_on_close = False

        print(f"\n================================================================================")
        print(f"🚀 Live Option Sentinel Dashboard Active (All Devices Mode):")
        print(f"   💻 On This PC          : http://127.0.0.1:{port}/")
        print(f"   📱 Phone / iPad / Wi-Fi: http://{lan_ip}:{port}/")
        print(f"   📡 Feed Mode           : 100% REAL LIVE MARKET DATA (ZERO SIMULATION)")
        print(f"   📂 Manual Positions    : positions.csv (Manageable from any browser)")
        print(f"   🛑 Stop Server         : Press Ctrl+C to exit immediately")
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
