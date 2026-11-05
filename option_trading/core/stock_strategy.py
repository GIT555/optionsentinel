"""
Institutional Direct Stock Buying & Accumulation Signal Engine.
Evaluates Dollar-Cost-Averaging (DCA) tranches, RSI oversold dip zones,
and flags leveraged ETF swing caveats.
"""

from __future__ import annotations
from typing import Tuple


def evaluate_stock_buy_signal(
    symbol: str, spot: float, rsi: float, iv_rank: float, is_leveraged: bool
) -> Tuple[str, str, str]:
    """
    Evaluates institutional direct stock buying criteria.
    Returns:
        (signal_code, tranche_label, rationale)
        - signal_code: 'BUY_TRANCHE_2', 'BUY_TRANCHE_1', 'HOLD', 'TRIM_OVERBOUGHT'
        - tranche_label: Human-readable tranche allocation
        - rationale: Detailed quantitative reasoning
    """
    if is_leveraged:
        # Triple-leveraged ETFs suffer from daily beta slippage (volatility drag)
        if rsi < 30.0:
            return (
                "BUY_TRANCHE_1",
                "⚡ Tactical Swing DCA Only",
                f"Severe oversold (RSI {rsi:.1f}). Tactical bounce swing entry only (strict 3-5 day holding max, tight stop-loss). Leveraged volatility drag makes long-term buy & hold hazardous.",
            )
        elif rsi > 65.0:
            return (
                "TRIM_OVERBOUGHT",
                "✂️ Take Profit / Trim",
                f"Overbought momentum (RSI {rsi:.1f}). High volatility decay risk. Take profits on leveraged positions immediately.",
            )
        else:
            return (
                "HOLD",
                "⏸️ Avoid Buy & Hold (Neutral)",
                f"RSI {rsi:.1f} in neutral territory. Leveraged decay makes buy-and-hold suboptimal.",
            )

    # Core Equities, Index ETFs, and Macro Commodities
    if rsi < 30.0:
        return (
            "BUY_TRANCHE_2",
            "💰 Heavy Accumulate (Tranche 2 - 50%)",
            f"Extreme oversold condition (RSI {rsi:.1f}). Prime direct equity accumulation window. Zero option theta decay risk.",
        )
    elif rsi < 35.0:
        return (
            "BUY_TRANCHE_1",
            "🛒 Dip Accumulate (Tranche 1 - 33%)",
            f"Oversold pullback (RSI {rsi:.1f}). Strong initial tranche direct stock entry before mean reversion.",
        )
    elif rsi > 70.0:
        return (
            "TRIM_OVERBOUGHT",
            "✂️ Overbought (Trim / Write CC)",
            f"RSI {rsi:.1f} extended into overbought zone. Consider trimming shares or writing Covered Calls to capture premium.",
        )
    else:
        return (
            "HOLD",
            "⏸️ Hold Existing / Neutral",
            f"RSI {rsi:.1f} in healthy continuation range. Hold existing equity holdings; no need to chase entries.",
        )
