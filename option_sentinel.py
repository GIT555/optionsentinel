#!/usr/bin/env python3
"""
Option Sentinel Launcher (option_sentinel.py)
=============================================
Entry point for the Option Trading Dashboard HTTP Server & CLI Scanner.
All core modules, market feeds, trading engines, and UI templates are organized
under the `option_trading/` package.
"""

from option_trading.server.app import (
    main,
    QuantMath,
    WATCHLIST_MATRIX,
    StrategyScanner,
    GammaDangerSentinel,
    SentinelRequestHandler,
)

if __name__ == "__main__":
    main()
