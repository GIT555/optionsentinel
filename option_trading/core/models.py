"""
Domain models and dataclasses for options, telemetry, and strike recommendations.
"""

from __future__ import annotations
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


def parse_occ_expiration(contract_symbol: str) -> Optional[str]:
    """Extracts ISO YYYY-MM-DD expiration date from an OCC contract symbol (e.g. TLT261127P00078000 -> 2026-11-27)."""
    if not contract_symbol:
        return None
    m = re.search(r"([A-Z]+)(\d{2})(\d{2})(\d{2})[CP]", contract_symbol.upper())
    if m:
        yy, mm, dd = m.group(2), m.group(3), m.group(4)
        return f"20{yy}-{mm}-{dd}"
    return None


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
    stock_buy_signal: str = "HOLD"            # BUY_TRANCHE_1, BUY_TRANCHE_2, HOLD, TRIM_OVERBOUGHT
    stock_buy_tranche: str = "Hold / Monitor"
    stock_buy_rationale: str = ""


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
