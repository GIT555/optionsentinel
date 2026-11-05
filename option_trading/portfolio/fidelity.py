#!/usr/bin/env python3
"""
Fidelity Portfolio CSV -> Option Sentinel positions.csv Converter
=================================================================
Automatically locates the latest Fidelity CSV export inside the `Fidelity/`
folder, parses all option contracts (OCC symbols, expirations, strikes,
Call/Put types, quantities, and premiums), and writes `positions.csv`.

Supports both Fidelity export layouts:
1. Fidelity "Options" View CSV (contains Call/put, Expiration, Intrinsic value, Time value, Delta)
2. Fidelity "Overview / Positions" View CSV (contains Quantity, Last Price, Average Cost Basis)
"""

from __future__ import annotations

import argparse
import csv
import json
import datetime
import glob
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


OCC_REGEX = re.compile(r"^-?([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d+(?:\.\d+)?)$")
DESC_REGEX = re.compile(
    r"^([A-Z]+)\s+([A-Z]{3}\s+\d{1,2}\s+\d{4})\s+\$(\d+(?:\.\d+)?)\s+(CALL|PUT)",
    re.IGNORECASE,
)

# Core watched tickers shared with Option Sentinel (zero additional API calls)
WATCHED_EQUITY_SYMBOLS = {
    "SPY", "QQQ", "XSP", "QQQM", "SPYM",
    "TQQQ", "SOXL", "SPXL",
    "GLD", "SLV", "IAU", "IBIT", "TLT",
}


@dataclass
class ConvertedOptionPosition:
    account_number: str
    account_name: str
    symbol: str
    strike: float
    option_type: str  # "CALL" or "PUT"
    expiration: str   # "YYYY-MM-DD"
    contracts: int    # < 0 for short options, > 0 for long options
    entry_premium: float
    current_mark: float
    delta: Optional[float] = None
    occ_symbol: str = ""


def clean_currency(val: str) -> Optional[float]:
    """Parses Fidelity currency/number strings like '$76.06', '--', '+$12.34', '($5.00)'."""
    if not val:
        return None
    s = val.strip()
    if s in ("--", "", "N/A", "n/a"):
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1]
    s = s.replace("$", "").replace(",", "").replace("%", "").replace("+", "").strip()
    if s.startswith("-"):
        negative = True
        s = s[1:].strip()
    try:
        num = float(s)
        return -num if negative else num
    except ValueError:
        return None


def parse_fidelity_date(date_str: str) -> Optional[str]:
    """Converts Fidelity date strings like 'Nov-20-2026' or 'NOV 20 2026' to 'YYYY-MM-DD'."""
    if not date_str or date_str.strip() in ("--", ""):
        return None
    cleaned = date_str.strip().title()
    for fmt in ("%b-%d-%Y", "%b %d %Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            dt = datetime.datetime.strptime(cleaned, fmt).date()
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_occ_symbol(raw_symbol: str) -> Optional[Tuple[str, str, str, float]]:
    """
    Parses Fidelity OCC option symbol such as ' -SOXL261120P160' or '-IBIT271217C25'.
    Returns (underlying, expiration_yyyy_mm_dd, option_type, strike).
    """
    s = raw_symbol.strip().upper().replace(" ", "")
    m = OCC_REGEX.match(s)
    if not m:
        return None
    root, yy, mm, dd, cp, strike_str = m.groups()
    exp_str = f"20{yy}-{mm}-{dd}"
    opt_type = "CALL" if cp == "C" else "PUT"
    strike = float(strike_str)
    return root, exp_str, opt_type, strike


def find_latest_fidelity_csv(fidelity_dir: str) -> Optional[str]:
    """Finds the most recently modified CSV file inside the Fidelity folder."""
    if not os.path.exists(fidelity_dir):
        return None
    pattern = os.path.join(fidelity_dir, "*.csv")
    files = glob.glob(pattern)
    if not files:
        return None
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return files[0]


def infer_contract_direction(
    expiration_yyyy_mm_dd: str,
    explicit_qty: Optional[float],
    reference_date: Optional[datetime.date] = None,
) -> int:
    """
    Determines signed contract count:
    - If Fidelity CSV provides an explicit Quantity column, uses int(round(explicit_qty)).
    - If exported from Fidelity's Options Greeks view (which omits Quantity),
      options with DTE > 120 are treated as Long LEAPs (+1) and options with DTE <= 120
      are treated as Short Wheel CSP / PMCC legs (-1).
    """
    if explicit_qty is not None and abs(explicit_qty) > 0:
        return int(round(explicit_qty))

    today = reference_date or datetime.date.today()
    try:
        exp_dt = datetime.datetime.strptime(expiration_yyyy_mm_dd, "%Y-%m-%d").date()
        dte = (exp_dt - today).days
    except ValueError:
        dte = 30

    return 1 if dte > 120 else -1


def load_existing_equity_overrides(positions_csv_path: str) -> Dict[Tuple[str, str], Tuple[int, float]]:
    """
    Reads existing EQUITY rows from positions.csv so that user-customized share
    counts and cost-basis values are preserved when importing a Fidelity 'Options'
    view CSV (which lists ETFs/Stocks by account but omits Quantity/Cost columns).
    """
    overrides: Dict[Tuple[str, str], Tuple[int, float]] = {}
    if not positions_csv_path or not os.path.exists(positions_csv_path):
        return overrides
    try:
        with open(positions_csv_path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            for row in reader:
                if not row or row[0].startswith("#") or row[0].lower() == "symbol":
                    continue
                if len(row) >= 6 and row[2].strip().upper() in ("EQUITY", "SHARES", "STOCK"):
                    sym = row[0].strip().upper()
                    qty = int(float(row[4]))
                    cost = float(row[5])
                    acct = row[6].strip() if len(row) > 6 else "Fidelity"
                    overrides[(acct, sym)] = (qty, cost)
    except Exception:
        pass
    return overrides


def parse_fidelity_csv(
    csv_path: str,
    reference_date: Optional[datetime.date] = None,
    include_watched_equities: bool = True,
    existing_positions_csv: Optional[str] = None,
) -> List[ConvertedOptionPosition]:
    """
    Reads a Fidelity Portfolio Positions CSV file and returns normalized option + watched equity positions.
    Aggregates identical option contracts or equity lots within the same account.
    """
    aggregated: Dict[Tuple[str, str, float, str, str], ConvertedOptionPosition] = {}
    equity_overrides = load_existing_equity_overrides(existing_positions_csv) if existing_positions_csv else {}
    seen_exact_rows = set()

    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row:
                continue

            # Normalize keys
            norm_row = {(k.strip().lower() if k else ""): (v.strip() if isinstance(v, str) else "") for k, v in row.items()}

            # 1. Exact duplicate line guard (prevents double-counting if rows were copy-pasted twice into the CSV)
            row_signature = (
                norm_row.get("account number", ""),
                norm_row.get("symbol", ""),
                norm_row.get("expiration", ""),
                norm_row.get("quantity", ""),
                norm_row.get("current value", ""),
                norm_row.get("cost basis total", ""),
                norm_row.get("account type", ""),
            )
            if any(row_signature) and row_signature in seen_exact_rows:
                continue
            seen_exact_rows.add(row_signature)

            raw_sym = norm_row.get("symbol", "")
            inv_type = norm_row.get("investment type", "")
            desc = norm_row.get("description", "")
            call_put = norm_row.get("call/put", "")
            acct_num = norm_row.get("account number", "").strip()
            acct_name = norm_row.get("account name", "").strip()

            # Check if this row is an Option contract
            occ_parsed = parse_occ_symbol(raw_sym)
            is_option = (
                inv_type.lower() == "options"
                or occ_parsed is not None
                or call_put.lower() in ("call", "put")
                or " CALL" in desc.upper()
                or " PUT" in desc.upper()
            )

            if not is_option:
                # Check if this is a watched Equity / ETF ticker (same tickers as Option Sentinel)
                clean_sym = raw_sym.strip().upper()
                is_equity_type = inv_type.lower() in ("etfs", "stocks", "equities", "")
                if include_watched_equities and is_equity_type and clean_sym in WATCHED_EQUITY_SYMBOLS:
                    explicit_qty = clean_currency(norm_row.get("quantity", ""))
                    last_price = clean_currency(norm_row.get("last price", "")) or 0.0
                    avg_cost = (
                        clean_currency(norm_row.get("average cost basis", ""))
                        or clean_currency(norm_row.get("cost basis per share", ""))
                    )
                    cost_total = clean_currency(norm_row.get("cost basis total", ""))

                    acct_label = acct_name if acct_name else "Fidelity"
                    if acct_num and acct_name == "ROTH IRA" and acct_num.endswith("0688"):
                        acct_label = "ROTH IRA (0688)"

                    override = equity_overrides.get((acct_label, clean_sym)) or equity_overrides.get((acct_name, clean_sym))

                    if explicit_qty is not None and abs(explicit_qty) > 0:
                        shares = int(round(explicit_qty))
                    elif override is not None:
                        shares = override[0]
                    else:
                        shares = 100  # Default 100-share covered-call lot when exported from Fidelity Options Greeks view

                    if avg_cost is not None and avg_cost > 0:
                        cost_per_share = round(avg_cost, 2)
                    elif cost_total is not None and shares > 0:
                        cost_per_share = round(abs(cost_total) / shares, 2)
                    elif override is not None and override[1] > 0:
                        cost_per_share = override[1]
                    else:
                        cost_per_share = round(last_price, 2) if last_price > 0 else 0.0

                    eq_key = (acct_label, clean_sym, 0.0, "EQUITY", "EQUITY")
                    if eq_key in aggregated:
                        existing_eq = aggregated[eq_key]
                        if explicit_qty is not None and abs(explicit_qty) > 0:
                            added_sh = int(round(explicit_qty))
                            total_sh = existing_eq.contracts + added_sh
                            if total_sh > 0 and cost_per_share > 0:
                                existing_eq.entry_premium = round(
                                    ((existing_eq.contracts * existing_eq.entry_premium) + (added_sh * cost_per_share)) / total_sh,
                                    2,
                                )
                            existing_eq.contracts = total_sh
                    else:
                        aggregated[eq_key] = ConvertedOptionPosition(
                            account_number=acct_num,
                            account_name=acct_label,
                            symbol=clean_sym,
                            strike=0.0,
                            option_type="EQUITY",
                            expiration="EQUITY",
                            contracts=shares,
                            entry_premium=cost_per_share,
                            current_mark=last_price,
                            delta=1.0,
                            occ_symbol=clean_sym,
                        )
                continue

            # Extract core contract identifiers
            if occ_parsed:
                symbol, expiration, option_type, strike = occ_parsed
            else:
                dm = DESC_REGEX.search(desc.upper())
                if not dm:
                    continue
                symbol = dm.group(1).upper()
                expiration = parse_fidelity_date(dm.group(2)) or parse_fidelity_date(norm_row.get("expiration", "")) or ""
                strike = float(dm.group(3))
                option_type = dm.group(4).upper()

            if not expiration:
                expiration = parse_fidelity_date(norm_row.get("expiration", "")) or ""
            if not expiration:
                continue

            # Quantity (if present in Overview export)
            explicit_qty = clean_currency(norm_row.get("quantity", ""))
            contracts = infer_contract_direction(expiration, explicit_qty, reference_date=reference_date)

            # Option mark & entry premium
            last_price = clean_currency(norm_row.get("last price", ""))
            intrinsic = clean_currency(norm_row.get("intrinsic value", "")) or 0.0
            time_val = clean_currency(norm_row.get("time value", "")) or 0.0
            computed_mark = round(intrinsic + time_val, 2)
            current_mark = last_price if (last_price is not None and last_price > 0) else computed_mark

            avg_cost = clean_currency(norm_row.get("average cost basis", "")) or clean_currency(norm_row.get("cost basis per share", ""))
            cost_total = clean_currency(norm_row.get("cost basis total", ""))
            if avg_cost is not None and avg_cost > 0:
                entry_premium = round(avg_cost, 2)
            elif cost_total is not None and abs(contracts) > 0:
                entry_premium = round(abs(cost_total) / (abs(contracts) * 100.0), 2)
            else:
                entry_premium = round(current_mark, 2) if current_mark > 0 else 1.00

            delta_val = clean_currency(norm_row.get("delta", ""))

            key = (acct_num, symbol, strike, option_type, expiration)
            if key in aggregated:
                existing = aggregated[key]
                step = explicit_qty if (explicit_qty is not None and abs(explicit_qty) > 0) else (1 if existing.contracts > 0 else -1)
                step_int = int(round(step))
                old_abs = abs(existing.contracts)
                add_abs = abs(step_int)
                if (old_abs + add_abs) > 0 and entry_premium > 0:
                    existing.entry_premium = round(
                        ((old_abs * existing.entry_premium) + (add_abs * entry_premium)) / (old_abs + add_abs),
                        2,
                    )
                existing.contracts += step_int
            else:
                aggregated[key] = ConvertedOptionPosition(
                    account_number=acct_num,
                    account_name=acct_name,
                    symbol=symbol,
                    strike=strike,
                    option_type=option_type,
                    expiration=expiration,
                    contracts=contracts,
                    entry_premium=entry_premium,
                    current_mark=current_mark,
                    delta=delta_val,
                    occ_symbol=raw_sym.strip(),
                )

    # Exclude equity lots with < 100 shares (must have at least 1 covered-call lot of 100 shares)
    return [
        pos for pos in aggregated.values()
        if not (pos.option_type == "EQUITY" and pos.contracts < 100)
    ]


def write_positions_csv(positions: List[ConvertedOptionPosition], output_path: str, source_csv_name: str) -> None:
    """Writes the converted option and equity positions into Option Sentinel's positions.csv format."""
    header_lines = [
        "# Manual Positions to Monitor (Live Option Sentinel)\n",
        f"# Auto-imported from Fidelity CSV: {source_csv_name} ({datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')})\n",
        "# Format: symbol,strike,option_type,expiration,contracts,entry_premium,account\n",
        "# option_type: PUT, CALL, or EQUITY (for shares of the same watched tickers)\n",
        "# contracts < 0 for short options (e.g. -1 for 1 sold put/call), > 0 for long LEAPs or EQUITY share count\n",
        "symbol,strike,option_type,expiration,contracts,entry_premium,account\n",
    ]

    with open(output_path, "w", encoding="utf-8", newline="") as f:
        for line in header_lines:
            f.write(line)
        for pos in positions:
            strike_fmt = f"{pos.strike:.2f}" if pos.strike % 1 != 0 else f"{int(pos.strike)}"
            acct_label = pos.account_name if pos.account_name else "Fidelity"
            f.write(
                f"{pos.symbol},{strike_fmt},{pos.option_type},{pos.expiration},"
                f"{pos.contracts},{pos.entry_premium:.2f},{acct_label}\n"
            )


def extract_fidelity_portfolio_balances(csv_path: str, output_json: Optional[str] = None) -> Dict[str, Any]:
    """
    Parses cash balances, money market sweeps (SPAXX, FDRXX), pending cash,
    and non-optionable assets (mutual funds, sub-100 shares) directly from Fidelity CSV.
    """
    if not os.path.exists(csv_path):
        return {}

    accounts: Dict[str, Dict[str, Any]] = {}
    TRADING_ACCTS = {"INDIVIDUAL - TOD", "ROTH IRA", "Rollover IRA", "Health Savings Account", "ROTH IRA (0688)"}

    with open(csv_path, "r", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.reader(f)
        header_row = None
        for raw_row in reader:
            if not raw_row or not any(cell.strip() for cell in raw_row):
                continue
            first_cell = raw_row[0].strip().lower()
            row_cells_lower = [c.strip().lower() for c in raw_row]
            if "account" in first_cell and ("symbol" in row_cells_lower or "description" in row_cells_lower):
                header_row = row_cells_lower
                break
        if not header_row:
            return {}

        col_map = {name: i for i, name in enumerate(header_row)}

        for raw_row in reader:
            if not raw_row or len(raw_row) < 3:
                continue
            first_clean = raw_row[0].strip()
            if first_clean.startswith('"The data and information') or first_clean.startswith('Brokerage services'):
                break

            def get_col(name: str) -> str:
                idx = col_map.get(name)
                return raw_row[idx].strip() if (idx is not None and idx < len(raw_row)) else ""

            acct_num = get_col("account number")
            acct_name = get_col("account name")
            if not acct_name and not acct_num:
                continue
            if acct_name == "ROTH IRA" and acct_num.endswith("0688"):
                acct_name = "ROTH IRA (0688)"

            if acct_name not in accounts:
                accounts[acct_name] = {
                    "account_number": acct_num,
                    "account_name": acct_name,
                    "is_trading_account": acct_name in TRADING_ACCTS,
                    "gross_cash": 0.0,
                    "pending_cash": 0.0,
                    "net_cash": 0.0,
                    "cash_sweep_symbol": "SPAXX",
                    "total_account_value": 0.0,
                    "mutual_funds_value": 0.0,
                    "mutual_funds_count": 0,
                    "equities_value": 0.0,
                }

            acc = accounts[acct_name]
            inv_type = get_col("investment type").lower()
            sym = get_col("symbol").upper()
            desc = get_col("description")
            curr_val = clean_currency(get_col("current value")) or 0.0

            acc["total_account_value"] += curr_val

            is_cash = (
                inv_type == "cash"
                or sym.startswith("SPAXX")
                or sym.startswith("FDRXX")
                or "HELD IN MONEY MARKET" in desc.upper()
            )
            is_pending = "PENDING ACTIVITY" in desc.upper() or "PENDING ACTIVITY" in sym.upper()

            if is_cash:
                acc["gross_cash"] += curr_val
                if "FDRXX" in sym or "FDRXX" in desc.upper():
                    acc["cash_sweep_symbol"] = "FDRXX"
            elif is_pending:
                acc["pending_cash"] += curr_val
            elif inv_type in ("mutual funds", "others"):
                acc["mutual_funds_value"] += curr_val
                acc["mutual_funds_count"] += 1
            elif inv_type in ("stocks", "etfs"):
                acc["equities_value"] += curr_val

    # Finalize per-account and global totals
    total_trading_gross_cash = 0.0
    total_trading_pending_cash = 0.0
    total_trading_net_cash = 0.0
    total_portfolio_net_worth = 0.0

    for a_name, a in accounts.items():
        a["gross_cash"] = round(a["gross_cash"], 2)
        a["pending_cash"] = round(a["pending_cash"], 2)
        a["net_cash"] = round(a["gross_cash"] + a["pending_cash"], 2)
        a["total_account_value"] = round(a["total_account_value"], 2)
        a["mutual_funds_value"] = round(a["mutual_funds_value"], 2)
        a["equities_value"] = round(a["equities_value"], 2)

        total_portfolio_net_worth += a["total_account_value"]
        if a["is_trading_account"]:
            total_trading_gross_cash += a["gross_cash"]
            total_trading_pending_cash += a["pending_cash"]
            total_trading_net_cash += a["net_cash"]

    apy = 0.0475  # Fidelity Money Market default APY (SPAXX ~4.75%)
    annual_yield = round(total_trading_net_cash * apy, 2)
    monthly_yield = round(annual_yield / 12.0, 2)

    summary = {
        "as_of_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_file": os.path.basename(csv_path),
        "total_trading_net_cash": round(total_trading_net_cash, 2),
        "total_trading_gross_cash": round(total_trading_gross_cash, 2),
        "total_trading_pending_cash": round(total_trading_pending_cash, 2),
        "total_portfolio_net_worth": round(total_portfolio_net_worth, 2),
        "cash_sweep_apy": apy,
        "annual_cash_yield_dollars": annual_yield,
        "monthly_cash_yield_dollars": monthly_yield,
        "accounts": accounts,
    }

    if output_json:
        try:
            with open(output_json, "w", encoding="utf-8") as f:
                json.dump(summary, f, indent=2)
        except Exception:
            pass

    return summary


def convert_latest_fidelity_to_positions(
    base_dir: Optional[str] = None,
    input_csv: Optional[str] = None,
    output_csv: Optional[str] = None,
) -> Tuple[str, List[ConvertedOptionPosition]]:
    """High-level helper callable from both CLI and the web server."""
    from ..config import BALANCES_FILE, FIDELITY_DIR, POSITIONS_FILE
    fidelity_dir = os.path.join(base_dir, "Fidelity") if base_dir else FIDELITY_DIR
    src_csv = input_csv or find_latest_fidelity_csv(fidelity_dir)
    if not src_csv or not os.path.exists(src_csv):
        raise FileNotFoundError(f"No Fidelity CSV found in {fidelity_dir}")

    dst_csv = output_csv or POSITIONS_FILE
    positions = parse_fidelity_csv(src_csv, existing_positions_csv=dst_csv)
    write_positions_csv(positions, dst_csv, os.path.basename(src_csv))

    balances_json = BALANCES_FILE
    try:
        extract_fidelity_portfolio_balances(src_csv, output_json=balances_json)
    except Exception:
        pass

    return src_csv, positions


def main():
    parser = argparse.ArgumentParser(description="Convert Fidelity Positions CSV to Option Sentinel positions.csv")
    parser.add_argument("--input", "-i", type=str, default=None, help="Path to specific Fidelity CSV file (defaults to latest in Fidelity/ folder)")
    parser.add_argument("--output", "-o", type=str, default=None, help="Output positions.csv path")
    args = parser.parse_args()

    src_csv, positions = convert_latest_fidelity_to_positions(input_csv=args.input, output_csv=args.output)

    print("=" * 95)
    print(f" FIDELITY CSV CONVERTER -> positions.csv")
    print(f" Source File : {src_csv}")
    print(f" Converted   : {len(positions)} Option Contracts")
    print("=" * 95)
    print(f"{'ACCOUNT':<20} {'SYMBOL':<7} {'TYPE':<6} {'STRIKE':<9} {'EXPIRATION':<12} {'QTY':<6} {'MARK/PREM':<10} {'DELTA'}")
    print("-" * 95)
    for p in positions:
        delta_str = f"{p.delta:+.4f}" if p.delta is not None else "N/A"
        print(
            f"{p.account_name[:19]:<20} {p.symbol:<7} {p.option_type:<6} "
            f"${p.strike:<8.2f} {p.expiration:<12} {p.contracts:<+6d} "
            f"${p.entry_premium:<9.2f} {delta_str}"
        )
    print("-" * 95)
    print(f"Successfully wrote {len(positions)} option positions to positions.csv")


if __name__ == "__main__":
    main()
