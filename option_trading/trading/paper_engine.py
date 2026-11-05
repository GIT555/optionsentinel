"""
Paper Trading Engine (paper_trading_engine.py)
================================================
Institutional Simulated Brokerage & Execution Engine.
- Real-time order execution against live exchange quotes.
- Full support for Cash-Secured Puts, Covered Calls, LEAPs, and Vertical Spreads.
- Local SQLite persistence (paper_trading.db) across server restarts.
- Auto-bracket tracking for 50% Profit Target and <=14 DTE Gamma Danger Rolls.
- Zero financial risk: 100% simulated capital.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import sqlite3
import threading
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple
from ..config import PAPER_DB_FILE


@dataclass
class PaperAccountSummary:
    initial_balance: float
    cash_balance: float
    locked_collateral: float
    available_buying_power: float
    net_liquidation_value: float
    unrealized_pnl: float
    realized_pnl: float
    total_pnl: float
    open_positions_count: int
    closed_positions_count: int


class PaperTradingEngine:
    """Thread-safe simulated brokerage engine persisting into paper_trading.db."""

    def __init__(self, db_path: Optional[str] = None, starting_cash: float = 100000.0):
        self.db_path = db_path or PAPER_DB_FILE
        self.starting_cash = starting_cash
        self._lock = threading.Lock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._lock, self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_account (
                    id INTEGER PRIMARY KEY,
                    initial_balance REAL NOT NULL,
                    cash_balance REAL NOT NULL,
                    realized_pnl REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_positions (
                    position_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    strategy_type TEXT NOT NULL,
                    legs_json TEXT NOT NULL,
                    entry_date TEXT NOT NULL,
                    expiration TEXT NOT NULL,
                    target_dte INTEGER NOT NULL,
                    contracts INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    current_mark REAL NOT NULL,
                    collateral_required REAL NOT NULL,
                    max_profit REAL NOT NULL,
                    max_risk REAL NOT NULL,
                    status TEXT NOT NULL, -- OPEN, CLOSED, ROLLED
                    exit_date TEXT,
                    exit_price REAL,
                    realized_pnl REAL DEFAULT 0.0,
                    notes TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_orders (
                    order_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    strategy_type TEXT NOT NULL,
                    order_action TEXT NOT NULL, -- BTO, STO, BTC, STC
                    contracts INTEGER NOT NULL,
                    fill_price REAL NOT NULL,
                    total_amount REAL NOT NULL,
                    collateral_delta REAL NOT NULL,
                    status TEXT NOT NULL, -- FILLED, REJECTED
                    details_json TEXT NOT NULL
                )
                """
            )

            # Ensure default account exists
            cur = conn.execute("SELECT id FROM paper_account WHERE id = 1")
            if not cur.fetchone():
                now_iso = datetime.datetime.now().isoformat()
                conn.execute(
                    """
                    INSERT INTO paper_account (id, initial_balance, cash_balance, realized_pnl, created_at, updated_at)
                    VALUES (1, ?, ?, 0.0, ?, ?)
                    """,
                    (self.starting_cash, self.starting_cash, now_iso, now_iso),
                )
            conn.commit()

    def get_account_summary(self) -> PaperAccountSummary:
        with self._lock, self._get_connection() as conn:
            acct_row = conn.execute("SELECT * FROM paper_account WHERE id = 1").fetchone()
            if not acct_row:
                initial = self.starting_cash
                cash = self.starting_cash
                realized = 0.0
            else:
                initial = float(acct_row["initial_balance"])
                cash = float(acct_row["cash_balance"])
                realized = float(acct_row["realized_pnl"])

            open_positions = conn.execute(
                "SELECT * FROM paper_positions WHERE status = 'OPEN'"
            ).fetchall()
            closed_count = conn.execute(
                "SELECT COUNT(*) FROM paper_positions WHERE status != 'OPEN'"
            ).fetchone()[0]

            total_collateral = 0.0
            total_unrealized = 0.0
            total_position_market_value = 0.0

            for p in open_positions:
                collateral = float(p["collateral_required"])
                entry_pr = float(p["entry_price"])
                curr_mark = float(p["current_mark"])
                qty = int(p["contracts"])
                strat = p["strategy_type"]

                total_collateral += collateral

                # Calculate unrealized P&L
                if "CREDIT" in strat or strat in ("CASH_SECURED_PUT", "COVERED_CALL", "IRON_CONDOR"):
                    # Sold for entry_pr, buy back at curr_mark
                    pnl = (entry_pr - curr_mark) * qty * 100.0
                    pos_val = -curr_mark * qty * 100.0
                else:
                    # Bought at entry_pr, sell at curr_mark
                    pnl = (curr_mark - entry_pr) * qty * 100.0
                    pos_val = curr_mark * qty * 100.0

                total_unrealized += pnl
                total_position_market_value += pos_val

            available_bp = max(0.0, cash - total_collateral)
            net_liq = cash + total_unrealized
            total_pnl = realized + total_unrealized

            return PaperAccountSummary(
                initial_balance=round(initial, 2),
                cash_balance=round(cash, 2),
                locked_collateral=round(total_collateral, 2),
                available_buying_power=round(available_bp, 2),
                net_liquidation_value=round(net_liq, 2),
                unrealized_pnl=round(total_unrealized, 2),
                realized_pnl=round(realized, 2),
                total_pnl=round(total_pnl, 2),
                open_positions_count=len(open_positions),
                closed_positions_count=closed_count,
            )

    def get_open_positions(self) -> List[Dict[str, Any]]:
        with self._lock, self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_positions WHERE status = 'OPEN' ORDER BY entry_date DESC"
            ).fetchall()
            results = []
            today = datetime.date.today()

            for r in rows:
                pos = dict(r)
                try:
                    pos["legs"] = json.loads(pos["legs_json"])
                except Exception:
                    pos["legs"] = []

                # Dynamic DTE calculation
                try:
                    exp_dt = datetime.datetime.strptime(pos["expiration"], "%Y-%m-%d").date()
                    dte = (exp_dt - today).days
                except Exception:
                    dte = int(pos.get("target_dte", 30))
                pos["dte"] = dte

                entry_p = float(pos["entry_price"])
                curr_p = float(pos["current_mark"])
                qty = int(pos["contracts"])
                strat = pos["strategy_type"]

                is_credit = "CREDIT" in strat or strat in ("CASH_SECURED_PUT", "COVERED_CALL", "IRON_CONDOR")
                if is_credit:
                    unrealized = (entry_p - curr_p) * qty * 100.0
                    pct = ((entry_p - curr_p) / entry_p * 100.0) if entry_p > 0 else 0.0
                    profit_hit = (pct >= 50.0)
                    gamma_danger = (dte <= 14)
                else:
                    unrealized = (curr_p - entry_p) * qty * 100.0
                    pct = ((curr_p - entry_p) / entry_p * 100.0) if entry_p > 0 else 0.0
                    profit_hit = (pct >= 50.0)
                    gamma_danger = False

                pos["unrealized_pnl"] = round(unrealized, 2)
                pos["pnl_pct"] = round(pct, 1)
                pos["profit_target_hit"] = profit_hit
                pos["gamma_danger"] = gamma_danger

                # Actionable badge status
                if profit_hit and gamma_danger:
                    pos["action_status"] = "EXIT NOW: 50% PROFIT + <=14 DTE"
                elif profit_hit:
                    pos["action_status"] = "TAKE PROFIT: 50% TARGET HIT"
                elif gamma_danger:
                    pos["action_status"] = "GAMMA DANGER: ROLL OUT"
                else:
                    pos["action_status"] = "SAFE THETA HARVEST"

                results.append(pos)
            return results

    def get_order_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock, self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_orders ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
            results = []
            for r in rows:
                o = dict(r)
                try:
                    o["details"] = json.loads(o["details_json"])
                except Exception:
                    o["details"] = {}
                results.append(o)
            return results

    def execute_paper_order(self, order_spec: Dict[str, Any]) -> Tuple[bool, str, Optional[str]]:
        """
        Executes a simulated paper order.
        Validates buying power / collateral requirements.
        Updates cash, collateral, orders, and positions tables.
        Returns: (success: bool, message: str, position_id: Optional[str])
        """
        sym = order_spec.get("symbol", "").upper().strip()
        strat = order_spec.get("strategy_type", "CASH_SECURED_PUT").upper().strip()
        qty = int(order_spec.get("contracts", 1))
        exp = order_spec.get("expiration", "").strip()
        dte = int(order_spec.get("target_dte", 30))
        price = float(order_spec.get("fill_price", order_spec.get("mid", 1.0)))
        collateral = float(order_spec.get("collateral_required", 0.0))
        max_profit = float(order_spec.get("max_profit", price * 100.0 * qty))
        max_risk = float(order_spec.get("max_risk", collateral))
        legs = order_spec.get("legs", [])
        notes = order_spec.get("notes", "")

        if qty <= 0:
            return False, "Contracts must be greater than 0.", None
        if not sym:
            return False, "Symbol is required.", None

        with self._lock, self._get_connection() as conn:
            acct = conn.execute("SELECT * FROM paper_account WHERE id = 1").fetchone()
            cash = float(acct["cash_balance"])

            # Check existing locked collateral
            open_rows = conn.execute("SELECT collateral_required FROM paper_positions WHERE status = 'OPEN'").fetchall()
            current_locked = sum(float(r["collateral_required"]) for r in open_rows)
            available_bp = cash - current_locked

            is_credit = "CREDIT" in strat or strat in ("CASH_SECURED_PUT", "COVERED_CALL", "IRON_CONDOR")

            # Check collateral requirements
            if is_credit and collateral > available_bp:
                return (
                    False,
                    f"Insufficient Buying Power. Required: ${collateral:,.2f}, Available: ${available_bp:,.2f}.",
                    None,
                )

            # Check debit cost
            if not is_credit:
                debit_cost = price * 100.0 * qty
                if debit_cost > cash:
                    return False, f"Insufficient cash for debit trade (${debit_cost:,.2f} required).", None

            now = datetime.datetime.now()
            now_str = now.strftime("%Y-%m-%d %H:%M:%S")
            now_iso = now.isoformat()
            pos_id = f"PAPER-{now.strftime('%m%d%H%M%S')}-{sym}"
            ord_id = f"ORD-{now.strftime('%m%d%H%M%S')}-{sym}"

            # Calculate cash and collateral delta
            if is_credit:
                cash_delta = price * 100.0 * qty
                collateral_delta = collateral
            else:
                cash_delta = -(price * 100.0 * qty)
                collateral_delta = 0.0

            new_cash = cash + cash_delta

            # 1. Insert position
            conn.execute(
                """
                INSERT INTO paper_positions (
                    position_id, symbol, strategy_type, legs_json, entry_date,
                    expiration, target_dte, contracts, entry_price, current_mark,
                    collateral_required, max_profit, max_risk, status, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
                """,
                (
                    pos_id,
                    sym,
                    strat,
                    json.dumps(legs),
                    now_str,
                    exp,
                    dte,
                    qty,
                    price,
                    price,  # current_mark starts at entry_price
                    collateral,
                    max_profit,
                    max_risk,
                    notes,
                ),
            )

            # 2. Record order
            order_action = "STO" if is_credit else "BTO"
            conn.execute(
                """
                INSERT INTO paper_orders (
                    order_id, timestamp, symbol, strategy_type, order_action,
                    contracts, fill_price, total_amount, collateral_delta, status, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'FILLED', ?)
                """,
                (
                    ord_id,
                    now_str,
                    sym,
                    strat,
                    order_action,
                    qty,
                    price,
                    abs(cash_delta),
                    collateral_delta,
                    json.dumps(order_spec),
                ),
            )

            # 3. Update account cash
            conn.execute(
                "UPDATE paper_account SET cash_balance = ?, updated_at = ? WHERE id = 1",
                (new_cash, now_iso),
            )
            conn.commit()

            msg = f"Paper Order FILLED: {order_action} {qty}x {sym} {strat} @ ${price:.2f} ({'+' if cash_delta >= 0 else ''}${cash_delta:,.2f})"
            return True, msg, pos_id

    def close_paper_position(self, position_id: str, exit_price: Optional[float] = None) -> Tuple[bool, str]:
        with self._lock, self._get_connection() as conn:
            pos = conn.execute(
                "SELECT * FROM paper_positions WHERE position_id = ? AND status = 'OPEN'", (position_id,)
            ).fetchone()
            if not pos:
                return False, f"Position {position_id} not found or already closed."

            entry_p = float(pos["entry_price"])
            mark = float(exit_price if exit_price is not None else pos["current_mark"])
            qty = int(pos["contracts"])
            strat = pos["strategy_type"]
            sym = pos["symbol"]
            is_credit = "CREDIT" in strat or strat in ("CASH_SECURED_PUT", "COVERED_CALL", "IRON_CONDOR")

            if is_credit:
                pnl = (entry_p - mark) * qty * 100.0
                cash_change = -(mark * qty * 100.0)  # Buy back
                order_action = "BTC"
            else:
                pnl = (mark - entry_p) * qty * 100.0
                cash_change = (mark * qty * 100.0)  # Sell to close
                order_action = "STC"

            now = datetime.datetime.now()
            now_str = now.strftime("%Y-%m-%d %H:%M:%S")
            now_iso = now.isoformat()
            ord_id = f"ORD-{now.strftime('%m%d%H%M%S')}-CLOSE"

            # 1. Update position status to CLOSED
            conn.execute(
                """
                UPDATE paper_positions
                SET status = 'CLOSED', exit_date = ?, exit_price = ?, realized_pnl = ?
                WHERE position_id = ?
                """,
                (now_str, mark, pnl, position_id),
            )

            # 2. Record closing order
            conn.execute(
                """
                INSERT INTO paper_orders (
                    order_id, timestamp, symbol, strategy_type, order_action,
                    contracts, fill_price, total_amount, collateral_delta, status, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'FILLED', ?)
                """,
                (
                    ord_id,
                    now_str,
                    sym,
                    strat,
                    order_action,
                    qty,
                    mark,
                    abs(cash_change),
                    -float(pos["collateral_required"]),
                    json.dumps({"closed_position_id": position_id, "realized_pnl": pnl}),
                ),
            )

            # 3. Update account cash and realized P&L
            acct = conn.execute("SELECT cash_balance, realized_pnl FROM paper_account WHERE id = 1").fetchone()
            new_cash = float(acct["cash_balance"]) + cash_change
            new_realized = float(acct["realized_pnl"]) + pnl

            conn.execute(
                "UPDATE paper_account SET cash_balance = ?, realized_pnl = ?, updated_at = ? WHERE id = 1",
                (new_cash, new_realized, now_iso),
            )
            conn.commit()

            return True, f"Closed {position_id} ({sym} {strat}): Realized P&L: {'+' if pnl >= 0 else ''}${pnl:,.2f}"

    def update_positions_marks(self, price_lookup_fn) -> int:
        """
        Updates current_mark for all open positions using the live market feed callback.
        Returns count of updated positions.
        """
        with self._lock, self._get_connection() as conn:
            rows = conn.execute("SELECT position_id, symbol, strategy_type, legs_json, entry_price FROM paper_positions WHERE status = 'OPEN'").fetchall()
            updated_count = 0

            for r in rows:
                pos_id = r["position_id"]
                sym = r["symbol"]
                strat = r["strategy_type"]
                try:
                    legs = json.loads(r["legs_json"])
                except Exception:
                    legs = []

                new_mark = price_lookup_fn(sym, strat, legs)
                if new_mark is not None and new_mark > 0:
                    conn.execute(
                        "UPDATE paper_positions SET current_mark = ? WHERE position_id = ?",
                        (round(new_mark, 2), pos_id),
                    )
                    updated_count += 1

            conn.commit()
            return updated_count

    def reset_account(self, new_balance: float = 100000.0):
        """Resets paper account back to starting state for a fresh test cycle."""
        with self._lock, self._get_connection() as conn:
            now_iso = datetime.datetime.now().isoformat()
            conn.execute("DELETE FROM paper_positions")
            conn.execute("DELETE FROM paper_orders")
            conn.execute(
                "UPDATE paper_account SET initial_balance = ?, cash_balance = ?, realized_pnl = 0.0, updated_at = ? WHERE id = 1",
                (new_balance, new_balance, now_iso),
            )
            conn.commit()
