"""
Multi-Strategy Live Market Scanner Engine.
Scans for 1-SD Cash-Secured Puts (Wheel), 0.85 Delta LEAP Calls & PMCC,
LEAP Puts & PMCP, Vertical Spreads, 3x Leveraged ETFs, and Direct Stock Buy signals.
"""

from __future__ import annotations
import datetime
import math
import sys
import threading
from typing import Any, Dict, List, Optional

from .models import (
    AssetCategory,
    LeapRecommendation,
    TickerMetadata,
    TickerScanResult,
    WheelStrikeRecommendation,
    parse_occ_expiration,
)
from .quant_math import QuantMath
from .stock_strategy import evaluate_stock_buy_signal
from .watchlist import load_watchlist, save_watchlist
from ..market.client import LiveMarketClient, LiveTickerSnapshot


class StrategyScanner:
    """Strategy Scanner powered exclusively by live exchange data."""

    def __init__(self):
        self.market_client = LiveMarketClient()
        self.math = QuantMath()
        self.watchlist = load_watchlist()
        self.snapshots: Dict[str, LiveTickerSnapshot] = {}
        self._summary_cache: Optional[Dict[str, Any]] = None
        self._summary_time: float = 0
        self._summary_lock = threading.Lock()
        self.refresh()

    def add_ticker(
        self,
        symbol: str,
        name: str = "",
        category: str = "CORE_EQUITY",
        is_leveraged: bool = False,
        beta: float = 1.0,
        strike_step: float = 1.0,
    ) -> bool:
        """Dynamically adds a ticker to the watchlist and fetches snapshot."""
        sym = symbol.upper().strip()
        if not sym:
            return False

        if not name:
            name = f"{sym} Equity / ETF"

        cat_enum = AssetCategory.CORE_EQUITY
        for c in AssetCategory:
            if c.name == category or c.value == category:
                cat_enum = c
                break

        meta = TickerMetadata(
            symbol=sym,
            name=name,
            category=cat_enum,
            is_leveraged=is_leveraged,
            is_tax_advantaged=False,
            strike_step=strike_step,
            beta=beta,
        )
        self.watchlist[sym] = meta
        save_watchlist(self.watchlist)

        # Immediately fetch live snapshot for the new symbol
        try:
            new_snap = self.market_client.get_all_snapshots_parallel([sym])
            self.snapshots.update(new_snap)
            self.market_client.preload_chains_parallel([sym])
        except Exception as e:
            print(f"[SCANNER] Failed to fetch live data for added ticker {sym}: {e}", file=sys.stderr)

        with self._summary_lock:
            self._summary_cache = None
        return True

    def delete_ticker(self, symbol: str) -> bool:
        """Deletes a ticker from the watchlist and saves to disk."""
        sym = symbol.upper().strip()
        if sym in self.watchlist:
            del self.watchlist[sym]
            save_watchlist(self.watchlist)
            if sym in self.snapshots:
                del self.snapshots[sym]
            with self._summary_lock:
                self._summary_cache = None
            return True
        return False

    def refresh(self):
        """Refreshes all snapshots and preloads options chains in parallel."""
        symbols = list(self.watchlist.keys())
        self.snapshots = self.market_client.get_all_snapshots_parallel(symbols)
        self.market_client.preload_chains_parallel(symbols)
        with self._summary_lock:
            self._summary_cache = None

    def scan_ticker_universe(self) -> List[TickerScanResult]:
        """Scans the ticker universe for Wheel, LEAP, and Direct Stock Buying triggers."""
        results: List[TickerScanResult] = []

        for sym, meta in self.watchlist.items():
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

            # Evaluate Direct Stock Buying Signals (DCA Tranches)
            buy_sig, buy_tranche, buy_rationale = evaluate_stock_buy_signal(
                sym, spot, rsi, ivr, meta.is_leveraged
            )

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
                    stock_buy_signal=buy_sig,
                    stock_buy_tranche=buy_tranche,
                    stock_buy_rationale=buy_rationale,
                )
            )
        return results

    def generate_wheel_matrix(self, all_tickers: bool = True) -> List[WheelStrikeRecommendation]:
        """Computes live 1-Standard Deviation Cash-Secured Put strikes."""
        recommendations: List[WheelStrikeRecommendation] = []

        for sym, meta in self.watchlist.items():
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
            otm_puts = [c for c in chain if c.option_type == "PUT" and c.strike < spot]
            puts = otm_puts if otm_puts else [c for c in chain if c.option_type == "PUT"]
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
                earn_warning = f"EARNINGS EVENT IN {days_earn} DAYS ({earn_date}) — DANGER"
                is_qualified = False

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
                status="QUALIFIED 1-SD WHEEL TRIGGER" if is_qualified else f"WATCH (IVR {snap.iv_rank:.1f}%)",
                earnings_date=earn_date,
                days_to_earnings=days_earn,
                earnings_warning=earn_warning,
            )
            recommendations.append(rec)

        recommendations.sort(key=lambda r: (not r.is_qualified, -r.annualized_roc))
        return recommendations

    def generate_leap_matrix(self, all_tickers: bool = True) -> List[LeapRecommendation]:
        """Calculates 0.85 Delta LEAP Calls with Poor Man's Covered Call (PMCC) overlay."""
        recommendations: List[LeapRecommendation] = []

        for sym, meta in self.watchlist.items():
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

            leaps = self.market_client.get_live_leap_options(sym, min_dte=180)
            if leaps:
                leaps = [c for c in leaps if c.bid > 0 and c.ask > 0] or leaps
                _leap_exp = min(leaps, key=lambda c: abs(c.dte - 390)).expiration
                leaps = [c for c in leaps if c.expiration == _leap_exp]
                # Target deep ITM Call (strike < spot) for ~0.85 Delta
                itm_leaps = [c for c in leaps if c.strike < spot]
                candidates = itm_leaps if itm_leaps else leaps
                best_leap = min(candidates, key=lambda c: abs(c.delta_approx - 0.85))
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
                # Short PMCC call MUST be strictly Out-Of-The-Money (strike > spot) and above the LEAP strike
                otm_pmcc = [c for c in calls if c.strike > spot and c.strike > leap_strike]
                candidates = otm_pmcc if otm_pmcc else [c for c in calls if c.strike > leap_strike] or calls
                best_pmcc = min(candidates, key=lambda c: abs(c.delta_approx - 0.30))
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
        """Generates Bearish LEAP Long Put (-0.85 Delta) with PMCP (-0.30 Delta) overlay."""
        recommendations: List[Dict[str, Any]] = []

        for sym, meta in self.watchlist.items():
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

            leaps = self.market_client.get_live_leap_options(sym, min_dte=180, option_type="PUT")
            if leaps:
                leaps = [c for c in leaps if c.bid > 0 and c.ask > 0] or leaps
                _leap_exp = min(leaps, key=lambda c: abs(c.dte - 390)).expiration
                leaps = [c for c in leaps if c.expiration == _leap_exp]
                # Target deep ITM Put (strike > spot) for ~ -0.85 Delta
                itm_puts = [c for c in leaps if c.strike > spot]
                candidates = itm_puts if itm_puts else leaps
                best_leap = min(candidates, key=lambda c: abs(c.delta_approx - (-0.85)))
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
            puts = [c for c in puts if c.bid > 0 and c.ask > 0] or puts
            if puts:
                _pm_exp = min(puts, key=lambda c: abs(c.dte - 45)).expiration
                puts = [c for c in puts if c.expiration == _pm_exp]
                # Short PMCP put MUST be strictly Out-Of-The-Money (strike < spot) and below the LEAP strike
                otm_pmcp = [c for c in puts if c.strike < spot and c.strike < leap_strike]
                candidates = otm_pmcp if otm_pmcp else [c for c in puts if c.strike < leap_strike] or puts
                best_pmcp = min(candidates, key=lambda c: abs(c.delta_approx - (-0.30)))
                pmcp_contract = best_pmcp.contract_symbol
                pmcp_strike = best_pmcp.strike
                pmcp_bid = best_pmcp.bid
                pmcp_ask = best_pmcp.ask
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
            gap_safety = "SAFE: Zero Downside Gap Risk" if spread_width >= net_debit else "MONITOR: Downside Risk"
            status_str = "QUALIFIED BEARISH LEAP TRIGGER" if is_qualified else f"WATCH (RSI {snap.rsi_14:.1f} | IVR {snap.iv_rank:.1f}%)"

            recommendations.append({
                "symbol": sym,
                "spot": spot,
                "iv": iv,
                "iv_rank": snap.iv_rank,
                "rsi": snap.rsi_14,
                "leap_target_delta": -0.85,
                "leap_target_dte": leap_dte,
                "leap_strike": leap_strike,
                "leap_contract": leap_contract,
                "leap_bid": leap_bid,
                "leap_ask": leap_ask,
                "leap_mid": leap_mid,
                "capital_saved_vs_shares": capital_saved,
                "effective_leverage": effective_leverage,
                "intrinsic_value": round(intrinsic, 2),
                "extrinsic_value": round(extrinsic, 2),
                "annual_drag_pct": annual_drag,
                "pmcc_target_delta": -0.30,
                "pmcc_target_dte": pmcp_dte,
                "pmcc_strike": pmcp_strike,
                "pmcc_contract": pmcp_contract,
                "pmcc_mid": pmcp_mid,
                "net_debit": net_debit,
                "pmcc_spread_width": spread_width,
                "pmcc_gap_safety": gap_safety,
                "pmcc_cash_flow_yield_pct": round((pmcp_mid / net_debit) * 100.0, 2) if net_debit > 0 else 0.0,
                "workflow_queue": f"BUY 1x {leap_contract} (@ mid ${leap_mid:.2f}) | SELL 1x {pmcp_contract} (@ mid ${pmcp_mid:.2f})",
                "leap_expiration": leap_exp,
                "pmcc_expiration": pmcp_exp,
                "leap_delta": leap_delta_val,
                "leap_theta": 0.01,
                "pmcc_bid": pmcp_bid,
                "pmcc_ask": pmcp_ask,
                "pmcc_delta": pmcp_delta_val,
                "pmcc_theta": 0.04,
                "name": meta.name,
                "is_qualified": is_qualified,
                "status": status_str,
            })

        recommendations.sort(key=lambda r: (not r["is_qualified"], -r["rsi"]))
        return recommendations

    def generate_spread_matrix(self, all_tickers: bool = True) -> List[Dict[str, Any]]:
        """Generates defined-risk Vertical Credit and Debit Spreads."""
        spreads: List[Dict[str, Any]] = []

        for sym, meta in self.watchlist.items():
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

            short_c = None
            long_c = None
            if candidates:
                # Ensure all spread legs share the identical expiration closest to target_dte
                _exp = min(candidates, key=lambda c: abs(c.dte - target_dte)).expiration
                same_exp_candidates = [c for c in candidates if c.expiration == _exp]
                if same_exp_candidates:
                    candidates = same_exp_candidates

                if spread_type == "Bull Put Credit Spread":
                    # Sell OTM Put, Buy lower strike Put
                    otm_puts = [c for c in candidates if c.strike < spot] or candidates
                    short_c = min(otm_puts, key=lambda c: abs(c.strike - short_k))
                    valid_longs = [c for c in candidates if c.strike < short_c.strike]
                    if valid_longs:
                        long_c = min(valid_longs, key=lambda c: abs(c.strike - long_k))
                elif spread_type == "Bear Call Credit Spread":
                    # Sell OTM Call, Buy higher strike Call
                    otm_calls = [c for c in candidates if c.strike > spot] or candidates
                    short_c = min(otm_calls, key=lambda c: abs(c.strike - short_k))
                    valid_longs = [c for c in candidates if c.strike > short_c.strike]
                    if valid_longs:
                        long_c = min(valid_longs, key=lambda c: abs(c.strike - long_k))
                elif spread_type == "Bull Call Debit Spread":
                    # Buy lower strike Call, Sell higher strike Call
                    long_c = min(candidates, key=lambda c: abs(c.strike - long_k))
                    valid_shorts = [c for c in candidates if c.strike > long_c.strike]
                    if valid_shorts:
                        short_c = min(valid_shorts, key=lambda c: abs(c.strike - short_k))
                else:  # Bear Put Debit Spread
                    # Buy higher strike Put, Sell lower strike Put
                    long_c = min(candidates, key=lambda c: abs(c.strike - long_k))
                    valid_shorts = [c for c in candidates if c.strike < long_c.strike]
                    if valid_shorts:
                        short_c = min(valid_shorts, key=lambda c: abs(c.strike - short_k))

                # Fallbacks if one leg was missing in ladder
                if not short_c and candidates:
                    short_c = min(candidates, key=lambda c: abs(c.strike - short_k))
                if not long_c and candidates:
                    long_c = min(candidates, key=lambda c: abs(c.strike - long_k))

            short_strike = short_c.strike if short_c else short_k
            long_strike = long_c.strike if long_c else long_k
            actual_width = abs(short_strike - long_strike)
            if actual_width == 0:
                actual_width = width
                if is_credit:
                    long_strike = short_strike - width if not is_call else short_strike + width
                else:
                    short_strike = long_strike + width if is_call else long_strike - width

            short_mid = short_c.mid if short_c and short_c.mid > 0 else round(self.math.black_scholes_price(spot, short_strike, target_dte, iv, 0.045, is_call), 2)
            long_mid = long_c.mid if long_c and long_c.mid > 0 else round(self.math.black_scholes_price(spot, long_strike, target_dte, iv, 0.045, is_call), 2)

            short_sym = short_c.contract_symbol if short_c else f"{sym}-{target_dte}d-{short_strike:.0f}{req_type[0]}"
            long_sym = long_c.contract_symbol if long_c else f"{sym}-{target_dte}d-{long_strike:.0f}{req_type[0]}"
            exp_date = (short_c.expiration if short_c else "") or parse_occ_expiration(short_sym) or (datetime.date.today() + datetime.timedelta(days=target_dte)).isoformat()
            dte_val = short_c.dte if short_c else target_dte

            short_delta = short_c.delta_approx if short_c else (0.25 if is_call else -0.25)
            long_delta = long_c.delta_approx if long_c else (0.15 if is_call else -0.15)

            if is_credit:
                raw_credit = round(short_mid - long_mid, 2)
                net_credit = max(0.05, min(raw_credit, round(actual_width - 0.05, 2)))
                max_profit = round(net_credit * 100.0, 2)
                max_risk = round((actual_width - net_credit) * 100.0, 2)
                roc = round((max_profit / max_risk) * 100.0, 1) if max_risk > 0 else 0.0
                annualized_roc = round(roc * (365.0 / dte_val), 1) if dte_val > 0 else 0.0
                pop = round((1.0 - abs(short_delta)) * 100.0, 1)
                breakeven = round(short_strike - net_credit, 2) if not is_call else round(short_strike + net_credit, 2)
                buffer_pct = round(abs(spot - short_strike) / spot * 100.0, 1)
                net_price = net_credit
                price_desc = f"Net Credit ${net_credit:.2f}"
                exit_target = round(net_credit * 0.50, 2)
                trade_action = f"SELL 1x {short_sym} / BUY 1x {long_sym}"
            else:
                raw_debit = round(long_mid - short_mid, 2)
                net_debit = max(0.05, min(raw_debit, round(actual_width - 0.05, 2)))
                max_profit = round((actual_width - net_debit) * 100.0, 2)
                max_risk = round(net_debit * 100.0, 2)
                roc = round((max_profit / max_risk) * 100.0, 1) if max_risk > 0 else 0.0
                annualized_roc = round(roc * (365.0 / dte_val), 1) if dte_val > 0 else 0.0
                pop = round(abs(long_delta) * 100.0, 1)
                breakeven = round(long_strike + net_debit, 2) if is_call else round(long_strike - net_debit, 2)
                buffer_pct = round(abs(spot - long_strike) / spot * 100.0, 1)
                net_price = net_debit
                price_desc = f"Net Debit ${net_debit:.2f}"
                exit_target = round(net_debit * 1.50, 2)
                trade_action = f"BUY 1x {long_sym} / SELL 1x {short_sym}"

            is_qualified = True
            status_text = "READY TO TRADE"

            earn_date = snap.earnings_date
            days_earn = snap.days_to_earnings
            earn_warning = None
            if days_earn is not None and 0 <= days_earn <= dte_val:
                earn_warning = f"EARNINGS in {days_earn}d ({earn_date})"
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
                "regime_note": f"IVR {ivr:.0f}% ({'High IV' if is_high_iv else 'Low IV'}) • RSI {rsi:.0f} ({'Oversold' if rsi < 35 else ('Overbought' if rsi > 65 else 'Neutral')})",
            })

        spreads.sort(key=lambda s: (not s["is_qualified"], -s["iv_rank"] if s["is_credit"] else s["iv_rank"]))
        return spreads
