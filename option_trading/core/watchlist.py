"""
Dynamic Watchlist Manager with disk persistence to watchlist.json.
"""

from __future__ import annotations
import json
import os
import sys
from typing import Dict, List, Optional
from .models import AssetCategory, TickerMetadata
from ..config import WATCHLIST_FILE

DEFAULT_WATCHLIST_PATH = WATCHLIST_FILE

FALLBACK_WATCHLIST: Dict[str, TickerMetadata] = {
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


def load_watchlist(filepath: str = DEFAULT_WATCHLIST_PATH) -> Dict[str, TickerMetadata]:
    """Loads tickers from watchlist.json or falls back to defaults."""
    matrix: Dict[str, TickerMetadata] = {}
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                for item in data:
                    sym = str(item.get("symbol", "")).upper().strip()
                    if not sym:
                        continue
                    cat_val = item.get("category", "CORE_EQUITY")
                    category = AssetCategory.CORE_EQUITY
                    for c in AssetCategory:
                        if c.name == cat_val or c.value == cat_val:
                            category = c
                            break
                    matrix[sym] = TickerMetadata(
                        symbol=sym,
                        name=item.get("name", f"{sym} Equity"),
                        category=category,
                        is_leveraged=bool(item.get("is_leveraged", False)),
                        is_tax_advantaged=bool(item.get("is_index", False) or item.get("is_tax_advantaged", False)),
                        strike_step=float(item.get("strike_step", 1.0)),
                        beta=float(item.get("beta", 1.0)),
                    )
        except Exception as e:
            print(f"[WATCHLIST] Error reading {filepath}: {e}", file=sys.stderr)

    if not matrix:
        matrix = dict(FALLBACK_WATCHLIST)
        save_watchlist(matrix, filepath)

    return matrix


def save_watchlist(matrix: Dict[str, TickerMetadata], filepath: str = DEFAULT_WATCHLIST_PATH):
    """Saves watchlist items to watchlist.json."""
    data = []
    for sym, m in matrix.items():
        data.append({
            "symbol": m.symbol,
            "name": m.name,
            "category": m.category.name if hasattr(m.category, "name") else str(m.category),
            "is_leveraged": m.is_leveraged,
            "is_index": m.is_tax_advantaged,
            "strike_step": m.strike_step,
            "beta": m.beta,
        })
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[WATCHLIST] Failed to save {filepath}: {e}", file=sys.stderr)
