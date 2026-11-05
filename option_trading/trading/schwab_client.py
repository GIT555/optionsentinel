"""
Schwab Trader API Integration (schwab_trader.py)
==================================================
Charles Schwab Trader API v1 Client & Order Builder.
- Account Discovery & Balance Inspection (GET /trader/v1/accounts)
- Safe Order Preview / Dry-Run Validation (POST /trader/v1/accounts/{hash}/previewOrder)
- Live Order Execution with Double Confirmation (POST /trader/v1/accounts/{hash}/orders)
- Formats standard 21-character OCC option symbols (e.g. 'SOXL  261120P00160000')
- Builds Single-Leg (CSP/CC) and Multi-Leg Vertical Spread Order Payloads
- Gracefully handles Market-Data-only developer apps without console 401 spam
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import sys
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple
from ..config import SCHWAB_TOKENS_FILE

try:
    import requests
except ImportError:
    requests = None


def format_schwab_occ_symbol(symbol: str, expiration_date: str, option_type: str, strike: float) -> str:
    """
    Formats standard 21-character OCC option symbol for Charles Schwab Trader API:
    - Root symbol: 6 characters (left-aligned, space-padded)
    - Expiration: YYMMDD (6 characters)
    - Option Type: 'C' or 'P' (1 character)
    - Strike: 8 digits (strike * 1000 zero-padded)
    Example: SOXL, '2026-11-20', 'PUT', 160.0 -> 'SOXL  261120P00160000'
    """
    root = symbol.strip().upper().ljust(6)
    try:
        dt = datetime.datetime.strptime(expiration_date.strip(), "%Y-%m-%d")
        yymmdd = dt.strftime("%y%m%d")
    except Exception:
        yymmdd = "261120"

    t = "C" if option_type.upper().startswith("C") else "P"
    strike_int = int(round(strike * 1000))
    strike_str = f"{strike_int:08d}"
    return f"{root}{yymmdd}{t}{strike_str}"


class SchwabTraderClient:
    """Official Schwab Trader API v1 Client."""

    def __init__(self, token_path: Optional[str] = None):
        self.token_path = token_path or SCHWAB_TOKENS_FILE
        self._lock = threading.Lock()
        self.app_key = ""
        self.app_secret = ""
        self.redirect_uri = "https://127.0.0.1"
        self._trader_enabled_flag: bool = False
        self._trader_authorized: Optional[bool] = None
        self._sync_env_credentials()

    def _sync_env_credentials(self):
        from ..market.client import _load_local_dotenv, get_app_credential
        dotenv_vars = _load_local_dotenv()
        self.app_key = get_app_credential("SCHWAB_APP_KEY", dotenv_vars)
        self.app_secret = get_app_credential("SCHWAB_APP_SECRET", dotenv_vars)
        redirect = (
            get_app_credential("SCHWAB_CALLBACK_URL", dotenv_vars)
            or get_app_credential("SCHWAB_REDIRECT_URI", dotenv_vars)
            or "https://127.0.0.1"
        )
        if "?code=" in redirect:
            redirect = redirect.split("?")[0]
        self.redirect_uri = redirect
        raw_trader = (
            get_app_credential("ENABLE_SCHWAB_TRADER", dotenv_vars)
            or get_app_credential("SCHWAB_TRADER_ENABLED", dotenv_vars)
            or "false"
        )
        self._trader_enabled_flag = raw_trader.strip().lower() in ("true", "1", "yes")

    @property
    def is_trader_enabled(self) -> bool:
        """
        Returns True only if explicitly opted in via ENABLE_SCHWAB_TRADER=true in .env.
        Defaults to False so users using Market Data only never encounter 401 logs or unwanted trader calls.
        """
        self._sync_env_credentials()
        return self._trader_enabled_flag

    def reset_authorization_cache(self):
        """Resets the cached authorization state (e.g. after re-authenticating with new scope)."""
        self._trader_authorized = None

    def _load_tokens(self) -> Dict[str, Any]:
        if os.path.exists(self.token_path):
            try:
                with open(self.token_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_tokens(self, tokens: Dict[str, Any]):
        with self._lock:
            try:
                with open(self.token_path, "w", encoding="utf-8") as f:
                    json.dump(tokens, f, indent=2)
            except Exception as e:
                sys.stderr.write(f"[SCHWAB-TRADER] Could not write tokens: {e}\n")

    def get_valid_access_token(self) -> Optional[str]:
        self._sync_env_credentials()
        if not (self.app_key and self.app_secret):
            return None

        tokens = self._load_tokens()
        now = time.time()
        access_token = tokens.get("access_token")
        access_exp = float(tokens.get("access_expires_at", 0.0))

        if access_token and now < access_exp:
            return access_token

        refresh_token = tokens.get("refresh_token")
        refresh_exp = float(tokens.get("refresh_expires_at", 0.0))
        if not refresh_token:
            return None
        if now >= refresh_exp:
            if (now - getattr(self, "_last_trader_warn", 0)) > 120:
                self._last_trader_warn = now
                exp_dt = datetime.datetime.fromtimestamp(refresh_exp).strftime("%Y-%m-%d %H:%M:%S") if refresh_exp > 0 else "unknown"
                sys.stderr.write(f"\n[SCHWAB-TRADER NOTICE]: Schwab token expired on {exp_dt}. Live trader routes disabled until re-auth.\n")
            return None

        basic_token = base64.b64encode(f"{self.app_key}:{self.app_secret}".encode("utf-8")).decode("utf-8")
        headers = {
            "Authorization": f"Basic {basic_token}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        try:
            resp = requests.post("https://api.schwabapi.com/v1/oauth/token", headers=headers, data=payload, timeout=6)
            if resp.status_code == 200:
                new_data = resp.json()
                tokens.update(new_data)
                tokens["access_expires_at"] = now + float(new_data.get("expires_in", 1800)) - 60.0
                self._save_tokens(tokens)
                return tokens.get("access_token")
        except Exception as e:
            sys.stderr.write(f"[SCHWAB-TRADER] Token refresh error: {e}\n")
        return None

    def get_account_numbers(self, force_check: bool = False) -> List[Dict[str, str]]:
        """
        Retrieves list of linked Schwab accounts with their encrypted hashValue.
        Endpoint: GET https://api.schwabapi.com/trader/v1/accounts/accountNumbers
        Gracefully suppresses HTTP 401/403 logs if the user is in Market-Data-only mode.
        """
        if not self.is_trader_enabled and not force_check:
            # Trader mode not enabled in .env; avoid unauthorized trader API calls
            return []

        if self._trader_authorized is False and not force_check:
            # Already verified unauthorized (Market Data app key); do not spam endpoint
            return []

        token = self.get_valid_access_token()
        if not token:
            return []

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        try:
            resp = requests.get("https://api.schwabapi.com/trader/v1/accounts/accountNumbers", headers=headers, timeout=5)
            if resp.status_code == 200:
                self._trader_authorized = True
                return resp.json()
            elif resp.status_code in (401, 403):
                # Account/Trading scope not provisioned (Market Data Only).
                # Mark unauthorized to cache and prevent repetitive console spam.
                self._trader_authorized = False
                return []
            else:
                sys.stderr.write(f"[SCHWAB-TRADER] accountNumbers HTTP {resp.status_code}: {resp.text[:150]}\n")
        except Exception as e:
            sys.stderr.write(f"[SCHWAB-TRADER] accountNumbers error: {e}\n")
        return []

    def get_account_balances(self, account_hash: str) -> Optional[Dict[str, Any]]:
        """
        Retrieves real-time buying power and positions for account.
        Endpoint: GET https://api.schwabapi.com/trader/v1/accounts/{accountHash}?fields=positions
        """
        if not self.is_trader_enabled or self._trader_authorized is False:
            return None

        token = self.get_valid_access_token()
        if not token:
            return None

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        url = f"https://api.schwabapi.com/trader/v1/accounts/{account_hash}?fields=positions"
        try:
            resp = requests.get(url, headers=headers, timeout=6)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            sys.stderr.write(f"[SCHWAB-TRADER] get_account_balances error: {e}\n")
        return None

    def preview_order(self, account_hash: str, order_payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Dry-run order validation without live routing (Schwab's official sandbox/preview).
        Endpoint: POST https://api.schwabapi.com/trader/v1/accounts/{accountHash}/previewOrder
        """
        if not self.is_trader_enabled:
            return {
                "status": "error",
                "message": "Schwab Live Trader is disabled (ENABLE_SCHWAB_TRADER=false). Use Paper Trading Engine.",
            }

        token = self.get_valid_access_token()
        if not token:
            return {"status": "error", "message": "No valid Schwab access token. Re-authenticate OAuth."}

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = f"https://api.schwabapi.com/trader/v1/accounts/{account_hash}/previewOrder"
        try:
            resp = requests.post(url, headers=headers, json=order_payload, timeout=7)
            if resp.status_code in (200, 201):
                data = resp.json()
                return {"status": "success", "preview": data}
            return {
                "status": "rejected",
                "http_code": resp.status_code,
                "error": resp.text[:300],
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def place_order(
        self, account_hash: str, order_payload: Dict[str, Any], live_confirm: bool = False
    ) -> Dict[str, Any]:
        """
        Submits real live order to Charles Schwab. Requires live_confirm=True for safety.
        Endpoint: POST https://api.schwabapi.com/trader/v1/accounts/{accountHash}/orders
        """
        if not self.is_trader_enabled:
            return {
                "status": "error",
                "message": "Schwab Live Trader is disabled (ENABLE_SCHWAB_TRADER=false). Use Paper Trading Engine.",
            }

        if not live_confirm:
            return {
                "status": "safety_block",
                "message": "Live order execution blocked: live_confirm must be explicitly True to trade real capital.",
            }

        token = self.get_valid_access_token()
        if not token:
            return {"status": "error", "message": "No valid Schwab access token."}

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = f"https://api.schwabapi.com/trader/v1/accounts/{account_hash}/orders"
        try:
            resp = requests.post(url, headers=headers, json=order_payload, timeout=8)
            if resp.status_code in (200, 201):
                order_loc = resp.headers.get("Location", "")
                order_id = order_loc.split("/")[-1] if order_loc else "SUBMITTED"
                return {"status": "placed", "order_id": order_id, "location": order_loc}
            return {
                "status": "rejected",
                "http_code": resp.status_code,
                "error": resp.text[:300],
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ==========================================================================
    # ORDER BUILDERS CONFORMING TO SCHWAB TRADER API V1 SPECIFICATION
    # ==========================================================================

    @staticmethod
    def build_csp_order(
        symbol: str, strike: float, expiration: str, limit_price: float, contracts: int = 1
    ) -> Dict[str, Any]:
        """Constructs a Cash-Secured Put order payload (Sell To Open Put)."""
        occ_sym = format_schwab_occ_symbol(symbol, expiration, "PUT", strike)
        return {
            "orderType": "LIMIT",
            "session": "NORMAL",
            "price": round(limit_price, 2),
            "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [
                {
                    "instruction": "SELL_TO_OPEN",
                    "quantity": abs(contracts),
                    "instrument": {
                        "symbol": occ_sym,
                        "assetType": "OPTION",
                    },
                }
            ],
        }

    @staticmethod
    def build_covered_call_order(
        symbol: str, strike: float, expiration: str, limit_price: float, contracts: int = 1
    ) -> Dict[str, Any]:
        """Constructs a Covered Call order payload (Sell To Open Call)."""
        occ_sym = format_schwab_occ_symbol(symbol, expiration, "CALL", strike)
        return {
            "orderType": "LIMIT",
            "session": "NORMAL",
            "price": round(limit_price, 2),
            "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [
                {
                    "instruction": "SELL_TO_OPEN",
                    "quantity": abs(contracts),
                    "instrument": {
                        "symbol": occ_sym,
                        "assetType": "OPTION",
                    },
                }
            ],
        }

    @staticmethod
    def build_vertical_spread_order(
        symbol: str,
        short_strike: float,
        long_strike: float,
        expiration: str,
        is_put: bool,
        is_credit: bool,
        net_price: float,
        contracts: int = 1,
    ) -> Dict[str, Any]:
        """Constructs a 2-leg Vertical Spread order payload for Schwab."""
        opt_type = "PUT" if is_put else "CALL"
        short_occ = format_schwab_occ_symbol(symbol, expiration, opt_type, short_strike)
        long_occ = format_schwab_occ_symbol(symbol, expiration, opt_type, long_strike)

        order_type = "NET_CREDIT" if is_credit else "NET_DEBIT"

        return {
            "orderType": order_type,
            "session": "NORMAL",
            "price": round(abs(net_price), 2),
            "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [
                {
                    "instruction": "SELL_TO_OPEN",
                    "quantity": abs(contracts),
                    "instrument": {
                        "symbol": short_occ,
                        "assetType": "OPTION",
                    },
                },
                {
                    "instruction": "BUY_TO_OPEN",
                    "quantity": abs(contracts),
                    "instrument": {
                        "symbol": long_occ,
                        "assetType": "OPTION",
                    },
                },
            ],
        }

    @staticmethod
    def build_long_option_order(
        symbol: str, strike: float, expiration: str, is_call: bool, limit_price: float, contracts: int = 1
    ) -> Dict[str, Any]:
        """Constructs a Long Option order payload (Buy To Open Call or Put, e.g. LEAP)."""
        occ_sym = format_schwab_occ_symbol(symbol, expiration, "CALL" if is_call else "PUT", strike)
        return {
            "orderType": "LIMIT",
            "session": "NORMAL",
            "price": round(limit_price, 2),
            "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [
                {
                    "instruction": "BUY_TO_OPEN",
                    "quantity": abs(contracts),
                    "instrument": {
                        "symbol": occ_sym,
                        "assetType": "OPTION",
                    },
                }
            ],
        }

    @staticmethod
    def build_diagonal_spread_order(
        symbol: str,
        long_strike: float,
        long_exp: str,
        short_strike: float,
        short_exp: str,
        is_call: bool,
        net_debit: float,
        contracts: int = 1,
    ) -> Dict[str, Any]:
        """Constructs a Diagonal Spread / PMCC order payload for Schwab (Long LEAP + Short near-term)."""
        opt_type = "CALL" if is_call else "PUT"
        long_occ = format_schwab_occ_symbol(symbol, long_exp, opt_type, long_strike)
        short_occ = format_schwab_occ_symbol(symbol, short_exp, opt_type, short_strike)
        return {
            "orderType": "NET_DEBIT",
            "session": "NORMAL",
            "price": round(abs(net_debit), 2),
            "duration": "DAY",
            "orderStrategyType": "DIAGONAL",
            "orderLegCollection": [
                {
                    "instruction": "BUY_TO_OPEN",
                    "quantity": abs(contracts),
                    "instrument": {
                        "symbol": long_occ,
                        "assetType": "OPTION",
                    },
                },
                {
                    "instruction": "SELL_TO_OPEN",
                    "quantity": abs(contracts),
                    "instrument": {
                        "symbol": short_occ,
                        "assetType": "OPTION",
                    },
                },
            ],
        }
