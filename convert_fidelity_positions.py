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

import argparse
import csv
import datetime
import glob
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


OCC_REGEX = re.compile(r"^-?([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d+(?:\.\d+)?)$")
DESC_REGEX = re.compile(
    r"^([A-Z]+)\s+([A-Z]{3}\s+\d{1,2}\s+\d{4})\s+\$(\d+(?:\.\d+)?)\s+(CALL|PUT)",
    re.IGNORECASE,
)


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


def parse_fidelity_csv(csv_path: str, reference_date: Optional[datetime.date] = None) -> List[ConvertedOptionPosition]:
    """
    Reads a Fidelity Portfolio Positions CSV file and returns normalized option positions.
    Aggregates identical option contracts within the same account if listed as separate lots.
    """
    aggregated: Dict[Tuple[str, str, float, str, str], ConvertedOptionPosition] = {}

    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row:
                continue

            # Normalize keys
            norm_row = {(k.strip().lower() if k else ""): (v.strip() if isinstance(v, str) else "") for k, v in row.items()}

            raw_sym = norm_row.get("symbol", "")
            inv_type = norm_row.get("investment type", "")
            desc = norm_row.get("description", "")
            call_put = norm_row.get("call/put", "")

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

            acct_num = norm_row.get("account number", "").strip()
            acct_name = norm_row.get("account name", "").strip()

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
                # Increment contract count in the same direction
                step = explicit_qty if (explicit_qty is not None and abs(explicit_qty) > 0) else (1 if existing.contracts > 0 else -1)
                existing.contracts += int(round(step))
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

    return list(aggregated.values())


def write_positions_csv(positions: List[ConvertedOptionPosition], output_path: str, source_csv_name: str) -> None:
    """Writes the converted option positions into Option Sentinel's positions.csv format."""
    header_lines = [
        "# Manual Positions to Monitor (Live Option Sentinel)\n",
        f"# Auto-imported from Fidelity CSV: {source_csv_name} ({datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')})\n",
        "# Format: symbol,strike,option_type,expiration,contracts,entry_premium,account\n",
        "# contracts < 0 for short options (e.g. -1 for 1 sold put/call), > 0 for long LEAPs\n",
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


def convert_latest_fidelity_to_positions(
    base_dir: Optional[str] = None,
    input_csv: Optional[str] = None,
    output_csv: Optional[str] = None,
) -> Tuple[str, List[ConvertedOptionPosition]]:
    """High-level helper callable from both CLI and the web server."""
    root_dir = base_dir or os.path.dirname(os.path.abspath(__file__))
    fidelity_dir = os.path.join(root_dir, "Fidelity")
    src_csv = input_csv or find_latest_fidelity_csv(fidelity_dir)
    if not src_csv or not os.path.exists(src_csv):
        raise FileNotFoundError(f"No Fidelity CSV found in {fidelity_dir}")

    dst_csv = output_csv or os.path.join(root_dir, "positions.csv")
    positions = parse_fidelity_csv(src_csv)
    write_positions_csv(positions, dst_csv, os.path.basename(src_csv))
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
