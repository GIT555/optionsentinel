"""
Portfolio Risk & Gamma Danger Sentinel.
Audits manual/imported positions, tracks <14 DTE Gamma acceleration,
calculates Net Beta-Weighted Delta, margin utilization, and Covered Call candidates.
"""

from __future__ import annotations
import csv
import datetime
import json
import math
import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from ..core.models import AssetCategory, parse_occ_expiration
from ..core.scanner import StrategyScanner
from ..config import POSITIONS_FILE


def calculate_hedged_put_collateral(positions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Calculates institutional hedged put collateral under standard Reg-T / Portfolio Margin rules.
    Detects when a short put is paired with a higher-strike long put (e.g. Bear Diagonal Spread / PMCP)
    or vertical spread, reducing the cash required from full strike collateral down to the actual defined risk.
    """
    sym_groups: Dict[str, List[Dict[str, Any]]] = {}
    for p in positions:
        sym = p.get("symbol")
        if sym:
            sym_groups.setdefault(sym, []).append(p)

    gross_collateral = 0.0
    effective_collateral = 0.0
    hedged_relief = 0.0
    hedged_details: List[str] = []

    for sym, s_pos in sym_groups.items():
        short_puts = [
            {
                "strike": float(p.get("strike", 0.0)),
                "contracts": abs(int(float(p.get("contracts", 0)))),
                "expiration": str(p.get("expiration", "")),
            }
            for p in s_pos
            if p.get("option_type") == "PUT" and float(p.get("contracts", 0)) < 0
        ]
        long_puts = [
            {
                "strike": float(p.get("strike", 0.0)),
                "available": abs(int(float(p.get("contracts", 0)))),
                "expiration": str(p.get("expiration", "")),
            }
            for p in s_pos
            if p.get("option_type") == "PUT" and float(p.get("contracts", 0)) > 0
        ]

        # Highest strike long put provides the best protective coverage
        long_puts.sort(key=lambda x: -x["strike"])

        for sp in short_puts:
            sp_strike = sp["strike"]
            qty_needed = sp["contracts"]
            sp_gross = sp_strike * 100.0 * qty_needed
            gross_collateral += sp_gross

            for lp in long_puts:
                if qty_needed <= 0:
                    break
                if lp["available"] <= 0:
                    continue
                match_qty = min(qty_needed, lp["available"])
                lp_strike = lp["strike"]

                if lp_strike >= sp_strike:
                    # Long put strike >= short put strike (PMCP / Diagonal / Calendar Spread)
                    # Zero downside gap risk; full strike collateral relief
                    spread_collat = 0.0
                    relief = sp_strike * 100.0 * match_qty
                    hedged_relief += relief
                    hedged_details.append(
                        f"{sym}: Short {sp_strike:.1f}P ({sp['expiration']}) fully hedged by Long {lp_strike:.1f}P ({lp['expiration']}) [Relief: ${relief:,.0f}]"
                    )
                else:
                    # Vertical credit spread (short strike > long strike)
                    # Defined-risk width collateral required
                    width = sp_strike - lp_strike
                    spread_collat = width * 100.0 * match_qty
                    relief = (sp_strike - width) * 100.0 * match_qty
                    hedged_relief += relief
                    hedged_details.append(
                        f"{sym}: Short {sp_strike:.1f}P ({sp['expiration']}) spread-hedged by Long {lp_strike:.1f}P ({lp['expiration']}) [Width: ${width:.1f}, Collateral: ${spread_collat:,.0f}]"
                    )

                effective_collateral += spread_collat
                qty_needed -= match_qty
                lp["available"] -= match_qty

            if qty_needed > 0:
                uncovered_collat = sp_strike * 100.0 * qty_needed
                effective_collateral += uncovered_collat

    return {
        "gross_collateral": round(gross_collateral, 2),
        "effective_collateral": round(effective_collateral, 2),
        "hedged_relief": round(hedged_relief, 2),
        "hedged_details": hedged_details,
    }


class GammaDangerSentinel:
    """Audits manual entries in positions.csv against live market feeds."""

    def __init__(self, scanner: StrategyScanner, positions_file: Optional[str] = None):
        self.scanner = scanner
        self.positions_file = os.path.abspath(positions_file or POSITIONS_FILE)
        self.alert_history: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def audit_manual_positions(self, emit_stderr: bool = True) -> List[Dict[str, Any]]:
        """Audits positions for Gamma Danger Zone (<14 DTE short options)."""
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
        """Returns all active positions in positions.csv with live market quotes, P&L, and Covered Call status."""
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

        acct_coverage: Dict[Tuple[str, str], Dict[str, int]] = {}
        for (r_sym, _k, r_type, _exp, r_qty, _prem, r_acct) in user_rows:
            cov_key = (r_sym, r_acct)
            if cov_key not in acct_coverage:
                acct_coverage[cov_key] = {"shares": 0, "leaps": 0, "short_calls": 0}
            if r_type in ("EQUITY", "SHARES", "STOCK"):
                acct_coverage[cov_key]["shares"] += r_qty
            elif r_type == "CALL":
                if r_qty < 0:
                    acct_coverage[cov_key]["short_calls"] += abs(r_qty)
                elif r_qty > 0:
                    acct_coverage[cov_key]["leaps"] += r_qty

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

                cov_item = acct_coverage.get((sym, acct_label), {"shares": contracts, "leaps": 0, "short_calls": 0})
                tot_lots = (cov_item["shares"] // 100) + cov_item["leaps"]
                act_sc = cov_item["short_calls"]
                uncovered_lots = max(0, tot_lots - act_sc)
                if act_sc >= tot_lots and tot_lots > 0:
                    status = f"CORE SHARES (COVERED: {act_sc}/{tot_lots} Calls Active)"
                elif snap and snap.rsi_14 < 35.0:
                    status = f"EQUITY DIP ZONE: RSI {snap.rsi_14:.1f} (<35 DCA)"
                elif snap and (snap.rsi_14 > 65.0 or snap.iv_rank > 40.0) and uncovered_lots > 0:
                    status = f"WRITE COVERED CALL ({uncovered_lots}x Uncovered • IVR {snap.iv_rank:.0f}%)"
                elif uncovered_lots > 0:
                    status = f"CORE EQUITY ({uncovered_lots}x CC ELIGIBLE)"
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
                })
                continue

            try:
                exp_date = datetime.datetime.strptime(exp_str, "%Y-%m-%d").date()
                dte = max(0, (exp_date - today).days)
            except Exception:
                dte = 30

            snap = self.scanner.snapshots.get(sym)
            matched = self.scanner.market_client.lookup_position_contract(sym, opt_type, strike, dte)
            curr_mid = matched.mid if matched else entry_prem
            gamma_val = matched.gamma_approx if matched else 0.02
            delta_val = matched.delta_approx if matched else (-0.25 if opt_type == "PUT" else 0.50)

            is_short = (contracts < 0)
            if is_short:
                pnl = (entry_prem - curr_mid) * abs(contracts) * 100.0
                pnl_pct = ((entry_prem - curr_mid) / entry_prem) * 100.0 if entry_prem > 0 else 0.0
                profit_target_hit = (curr_mid <= (entry_prem * 0.50))
            else:
                pnl = (curr_mid - entry_prem) * contracts * 100.0
                pnl_pct = ((curr_mid - entry_prem) / entry_prem) * 100.0 if entry_prem > 0 else 0.0
                profit_target_hit = (curr_mid >= (entry_prem * 1.50))

            gamma_danger = (is_short and dte <= 14)

            status = "HEALTHY / HOLD"
            if is_short:
                if profit_target_hit:
                    status = "TAKE PROFIT (>= 50% Profit Reached)"
                elif gamma_danger:
                    status = "GAMMA RISK (<14 DTE: Roll or Close)"
                elif dte <= 21:
                    status = "MONITOR CLOSELY (<21 DTE)"
            else:
                if profit_target_hit:
                    status = "TRIM LEAP (+50% Gain Hit)"
                elif dte <= 60:
                    status = "TIME DECAY ACCELERATING (Roll Out)"

            results.append({
                "index": idx,
                "position_id": f"MANUAL-{idx:02d}-{sym}",
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
                "delta": round(delta_val, 2),
                "gamma_danger": gamma_danger,
                "profit_target_hit": profit_target_hit,
                "status": status,
                "earnings_date": snap.earnings_date if snap else None,
                "days_to_earnings": snap.days_to_earnings if snap else None,
            })

        return results

    def evaluate_equity_and_exposure(self, positions: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Calculates cash reserves, collateral requirements, and buying power."""
        balances_path = os.path.join(os.path.dirname(self.positions_file), "portfolio_balances.json")
        cash_by_account = {}
        if os.path.exists(balances_path):
            try:
                with open(balances_path, "r", encoding="utf-8") as bf:
                    cash_by_account = json.load(bf)
            except Exception:
                pass

        accounts = sorted(list(set([p.get("account", "Manual") for p in positions] + list(cash_by_account.keys()))))
        account_summaries = {}

        total_portfolio_cash = 0.0
        total_collateral_committed = 0.0
        total_equity_value = 0.0

        for acct in accounts:
            acct_pos = [p for p in positions if p.get("account", "Manual") == acct]
            acct_cash = cash_by_account.get(acct, {}).get("cash_sweep", 0.0)
            total_portfolio_cash += acct_cash

            acct_collat_info = calculate_hedged_put_collateral(acct_pos)
            collateral_req = acct_collat_info["effective_collateral"]
            gross_collat = acct_collat_info["gross_collateral"]
            hedged_relief = acct_collat_info["hedged_relief"]
            equity_val = sum(p["current_mid"] * p["contracts"] for p in acct_pos if p.get("option_type") == "EQUITY")

            total_collateral_committed += collateral_req
            total_equity_value += equity_val
            free_cash = acct_cash - collateral_req
            utilization = (collateral_req / acct_cash * 100.0) if acct_cash > 0 else 0.0

            account_summaries[acct] = {
                "cash_sweep": round(acct_cash, 2),
                "collateral_committed": round(collateral_req, 2),
                "gross_collateral": round(gross_collat, 2),
                "hedged_relief": round(hedged_relief, 2),
                "free_cash": round(free_cash, 2),
                "equity_value": round(equity_val, 2),
                "utilization_pct": round(utilization, 1),
                "positions_count": len(acct_pos),
            }

        total_free_cash = total_portfolio_cash - total_collateral_committed
        overall_utilization = (total_collateral_committed / total_portfolio_cash * 100.0) if total_portfolio_cash > 0 else 0.0

        return {
            "total_portfolio_cash": round(total_portfolio_cash, 2),
            "total_collateral_committed": round(total_collateral_committed, 2),
            "total_free_cash": round(total_free_cash, 2),
            "total_equity_value": round(total_equity_value, 2),
            "overall_utilization_pct": round(overall_utilization, 1),
            "accounts": account_summaries,
        }

    def get_portfolio_beta_summary(self) -> Dict[str, Any]:
        """Calculates portfolio Net Beta-Weighted Delta against SPY."""
        positions = self.evaluate_all_manual_positions()
        spy_snap = self.scanner.snapshots.get("SPY")
        spy_spot = spy_snap.spot if spy_snap else 580.0

        total_beta_delta = 0.0
        details = []

        for p in positions:
            sym = p["symbol"]
            meta = self.scanner.watchlist.get(sym)
            beta = meta.beta if meta else 1.0
            snap = self.scanner.snapshots.get(sym)
            spot = snap.spot if snap else p.get("current_mid", 100.0)

            if p["option_type"] == "EQUITY":
                raw_delta = float(p["contracts"])
            else:
                raw_delta = float(p.get("delta", 0.0)) * float(p["contracts"]) * 100.0

            # Beta-weighted delta calculation
            b_delta = raw_delta * beta * (spot / spy_spot) if spy_spot > 0 else 0.0
            total_beta_delta += b_delta
            details.append({
                "symbol": sym,
                "contracts": p["contracts"],
                "type": p["option_type"],
                "beta": beta,
                "raw_delta": round(raw_delta, 1),
                "beta_weighted_delta": round(b_delta, 1),
            })

        return {
            "spy_spot": spy_spot,
            "net_beta_delta": round(total_beta_delta, 1),
            "interpretation": (
                "Bullish Net Exposure" if total_beta_delta > 50
                else ("Bearish Net Exposure" if total_beta_delta < -50 else "Delta Neutral Range")
            ),
            "positions_analyzed": len(details),
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
        """Adds a position to positions.csv."""
        with open(self.positions_file, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([symbol.upper(), strike, option_type.upper(), expiration, contracts, entry_premium, account])

    def delete_manual_position(self, index: int):
        """Removes a position by line index from positions.csv."""
        if not os.path.exists(self.positions_file):
            return

        with open(self.positions_file, "r", encoding="utf-8") as f:
            lines = f.readlines()

        valid_idx = 0
        new_lines = []
        for line in lines:
            if not line.strip() or line.startswith("#") or line.lower().startswith("symbol"):
                new_lines.append(line)
                continue
            valid_idx += 1
            if valid_idx != index:
                new_lines.append(line)

        with open(self.positions_file, "w", encoding="utf-8") as f:
            f.writelines(new_lines)


class AutoFidelitySyncer:
    """Background file watcher to auto-convert new Fidelity CSV exports."""

    def __init__(self, sentinel: GammaDangerSentinel, interval_seconds: int = 15):
        self.sentinel = sentinel
        self.interval = interval_seconds
        self.running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def _worker(self):
        while self.running:
            time.sleep(self.interval)
            try:
                from .fidelity import convert_latest_fidelity_to_positions
                src_csv, converted = convert_latest_fidelity_to_positions()
                if converted:
                    self.sentinel.audit_manual_positions(emit_stderr=False)
            except Exception:
                pass
