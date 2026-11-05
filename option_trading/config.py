"""
Centralized configuration and directory paths for the Option Trading Dashboard.
"""

from __future__ import annotations
import os

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
FIDELITY_DIR = os.path.join(PROJECT_ROOT, "Fidelity")
UI_DIR = os.path.join(os.path.dirname(__file__), "ui")

os.makedirs(DATA_DIR, exist_ok=True)

ENV_FILE = os.path.join(PROJECT_ROOT, ".env")
WATCHLIST_FILE = os.path.join(DATA_DIR, "watchlist.json")
POSITIONS_FILE = os.path.join(DATA_DIR, "positions.csv")
BALANCES_FILE = os.path.join(DATA_DIR, "portfolio_balances.json")
PAPER_DB_FILE = os.path.join(DATA_DIR, "paper_trading.db")
MARKET_CACHE_FILE = os.path.join(DATA_DIR, "market_cache.db")
SCHWAB_TOKENS_FILE = os.path.join(DATA_DIR, "schwab_tokens.json")
DASHBOARD_HTML_FILE = os.path.join(UI_DIR, "dashboard.html")
