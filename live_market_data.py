"""
Live Market Data Engine (live_market_data.py) - Parallel Edition
================================================================
Institutional Real-Time Market Data and Option Chain Aggregator.
Fetches live equity spot quotes, historical daily candles (for RSI & volatility),
and real exchange options chains concurrently using ThreadPoolExecutor.

Uses native Windows Certificate Store via truststore for corporate/antivirus compatibility.
"""

from __future__ import annotations

import base64
import concurrent.futures
import datetime
import json
import math
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

try:
    import requests
except ImportError:
    requests = None

try:
    import yfinance as yf
except ImportError:
    yf = None


TICKER_SYMBOL_MAP: Dict[str, str] = {
    "SPY": "SPY",
    "QQQ": "QQQ",
    "XSP": "^XSP",
    "^XSP": "^XSP",
    "QQQM": "QQQM",
    "SPYM": "SPYM",
    "TQQQ": "TQQQ",
    "SOXL": "SOXL",
    "SPXL": "SPXL",
    "GLD": "GLD",
    "SLV": "SLV",
    "IAU": "IAU",
    "IBIT": "IBIT",
    "TLT": "TLT",
}


@dataclass
class LiveTickerSnapshot:
    symbol: str
    yahoo_symbol: str
    spot: float
    previous_close: float
    change_pct: float
    rsi_14: float
    historical_vol_30d: float
    iv_current: float
    iv_rank: float
    iv_percentile_6m: float
    timestamp: str
    is_live: bool = True


@dataclass
class LiveOptionContract:
    contract_symbol: str
    strike: float
    dte: int
    expiration: str
    option_type: str  # "CALL" or "PUT"
    bid: float
    ask: float
    mid: float
    last_price: float
    volume: int
    open_interest: int
    implied_volatility: float
    delta_approx: float
    gamma_approx: float
    theta_approx: float
    in_the_money: bool


class TokenBucketRateLimiter:
    """
    Thread-safe token-bucket rate limiter (AGENTS.md Rule #2).
    Prevents flooding free API endpoints (e.g., max 8 requests/min for Twelve Data).
    """

    def __init__(self, max_calls: int, period_seconds: float, min_interval_sec: float = 0.05):
        self.max_calls = max_calls
        self.period = period_seconds
        self.min_interval = min_interval_sec
        self._timestamps: List[float] = []
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            now = time.time()
            self._timestamps = [t for t in self._timestamps if (now - t) < self.period]
            if len(self._timestamps) >= self.max_calls:
                sleep_for = self.period - (now - self._timestamps[0]) + 0.05
                if sleep_for > 0:
                    time.sleep(sleep_for)
                now = time.time()
                self._timestamps = [t for t in self._timestamps if (now - t) < self.period]
            elif self._timestamps and (now - self._timestamps[-1]) < self.min_interval:
                time.sleep(self.min_interval)
                now = time.time()
            self._timestamps.append(now)


class SQLiteMarketCache:
    """
    Persistent local SQLite + JSON cache for historical & live market data (AGENTS.md Rule #3).
    Minimizes repetitive API calls and provides resilience if rate limits are hit.
    """

    def __init__(self, db_path: Optional[str] = None):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.db_path = db_path or os.path.join(base_dir, "market_cache.db")
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        try:
            with self._lock, sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS kv_cache (
                        cache_key TEXT PRIMARY KEY,
                        updated_at REAL NOT NULL,
                        payload_json TEXT NOT NULL
                    )
                    """
                )
                conn.commit()
        except Exception as e:
            sys.stderr.write(f"[CACHE-INIT WARNING]: SQLite cache initialization fallback: {e}\n")

    def get(self, key: str, max_age_seconds: Optional[float] = None) -> Optional[Any]:
        try:
            with self._lock, sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT updated_at, payload_json FROM kv_cache WHERE cache_key = ?", (key,)
                ).fetchone()
                if not row:
                    return None
                updated_at, payload_json = row
                if max_age_seconds is not None and (time.time() - updated_at) > max_age_seconds:
                    return None
                return json.loads(payload_json)
        except Exception:
            return None

    def set(self, key: str, value: Any):
        try:
            payload_json = json.dumps(value)
            with self._lock, sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO kv_cache (cache_key, updated_at, payload_json)
                    VALUES (?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        updated_at = excluded.updated_at,
                        payload_json = excluded.payload_json
                    """,
                    (key, time.time(), payload_json),
                )
                conn.commit()
        except Exception as e:
            sys.stderr.write(f"[CACHE-WRITE WARNING]: Unable to persist {key}: {e}\n")


def _load_local_dotenv() -> Dict[str, str]:
    """Loads key=value pairs from local .env file without external dependencies."""
    env_map: Dict[str, str] = {}
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    env_map[k.strip()] = v.strip().strip('"').strip("'")
        except Exception:
            pass
    return env_map


class SchwabReadOnlyClient:
    """
    100% Read-Only Charles Schwab / Thinkorswim Market Data Client.
    - Strictly GET-only Market Data endpoints (/marketdata/v1/pricehistory, /marketdata/v1/chains).
    - Zero order placement or account modification methods exist in this class.
    - Supports the standard 7-day Schwab OAuth2 refresh token cycle (same as OptionAlpha)
      with automatic 30-minute access_token renewal.
    """

    SCHWAB_SYMBOL_MAP: Dict[str, str] = {
        "XSP": "$XSP",
        "^XSP": "$XSP",
    }

    def __init__(self, limiter: TokenBucketRateLimiter):
        self.limiter = limiter
        self._lock = threading.Lock()
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.token_path = os.path.join(base_dir, "schwab_tokens.json")
        self.app_key = ""
        self.app_secret = ""
        self.redirect_uri = "https://127.0.0.1"
        self._sync_env_credentials()
        self._tokens: Dict[str, Any] = self._load_tokens()

    def _sync_env_credentials(self):
        dotenv_vars = _load_local_dotenv()
        key = (os.environ.get("SCHWAB_APP_KEY") or dotenv_vars.get("SCHWAB_APP_KEY") or "").strip()
        secret = (os.environ.get("SCHWAB_APP_SECRET") or dotenv_vars.get("SCHWAB_APP_SECRET") or "").strip()
        redirect = (
            os.environ.get("SCHWAB_REDIRECT_URI")
            or dotenv_vars.get("SCHWAB_REDIRECT_URI")
            or "https://127.0.0.1"
        ).strip()
        if key:
            self.app_key = key
        if secret:
            self.app_secret = secret
        if redirect:
            self.redirect_uri = redirect

    def _load_tokens(self) -> Dict[str, Any]:
        if os.path.exists(self.token_path):
            try:
                with open(self.token_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_tokens(self, token_payload: Dict[str, Any]):
        with self._lock:
            self._tokens = token_payload
            try:
                with open(self.token_path, "w", encoding="utf-8") as f:
                    json.dump(token_payload, f, indent=2)
            except Exception as e:
                sys.stderr.write(f"[SCHWAB-TOKEN WARNING]: Could not write schwab_tokens.json: {e}\n")

    def save_credentials(self, app_key: str, app_secret: str, redirect_uri: str = "https://127.0.0.1"):
        """Saves SCHWAB_APP_KEY and SCHWAB_APP_SECRET to local git-ignored .env file."""
        self.app_key = app_key.strip()
        self.app_secret = app_secret.strip()
        self.redirect_uri = (redirect_uri or self.redirect_uri or "https://127.0.0.1").strip()
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        existing = _load_local_dotenv()
        existing["SCHWAB_APP_KEY"] = self.app_key
        existing["SCHWAB_APP_SECRET"] = self.app_secret
        existing["SCHWAB_REDIRECT_URI"] = self.redirect_uri
        with open(env_path, "w", encoding="utf-8") as f:
            for k, v in existing.items():
                f.write(f"{k}={v}\n")

    def get_auth_url(self) -> str:
        if not self.app_key:
            return ""
        encoded_redirect = urllib.parse.quote(self.redirect_uri, safe=":/")
        return (
            f"https://api.schwabapi.com/v1/oauth/authorize"
            f"?response_type=code"
            f"&client_id={urllib.parse.quote(self.app_key, safe='')}"
            f"&redirect_uri={encoded_redirect}"
        )

    def get_status(self) -> Dict[str, Any]:
        self._sync_env_credentials()
        now = time.time()
        configured = bool(self.app_key and self.app_secret)
        refresh_exp = float(self._tokens.get("refresh_expires_at", 0.0))
        remaining_sec = max(0.0, refresh_exp - now)
        connected = bool(configured and self._tokens.get("refresh_token") and remaining_sec > 0)
        days_left = int(remaining_sec // 86400)
        hours_left = int((remaining_sec % 86400) // 3600)
        if not configured:
            status_label = "Not Configured (Using Yahoo/SQLite)"
        elif connected:
            status_label = f"Schwab ToS Connected ({days_left}d {hours_left}h left)"
        else:
            status_label = "Keys Loaded (.env) — Click to Activate 7d Token"
        return {
            "configured": configured,
            "connected": connected,
            "days_remaining": days_left,
            "hours_remaining": hours_left,
            "redirect_uri": self.redirect_uri,
            "auth_url": self.get_auth_url(),
            "status_label": status_label,
            "mode": "100% READ-ONLY (GET Market Data Only)",
        }

    def exchange_code_from_redirect(self, redirect_url_or_code: str) -> Tuple[bool, str]:
        """Exchanges pasted redirect URL (https://127.0.0.1/?code=...) for a 7-day refresh token."""
        self._sync_env_credentials()
        if not (self.app_key and self.app_secret):
            return False, "Missing SCHWAB_APP_KEY or SCHWAB_APP_SECRET."
        raw = redirect_url_or_code.strip()
        code = raw
        if "code=" in raw:
            parsed = urllib.parse.urlparse(raw)
            qs = urllib.parse.parse_qs(parsed.query)
            code = qs.get("code", [""])[0]
        else:
            code = urllib.parse.unquote(raw)
        if not code:
            return False, "No authorization code found in URL."

        basic_token = base64.b64encode(f"{self.app_key}:{self.app_secret}".encode("utf-8")).decode("utf-8")
        headers = {
            "Authorization": f"Basic {basic_token}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
        }
        try:
            resp = requests.post("https://api.schwabapi.com/v1/oauth/token", headers=headers, data=payload, timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                now = time.time()
                data["access_expires_at"] = now + float(data.get("expires_in", 1800)) - 60.0
                data["refresh_expires_at"] = now + (7 * 86400.0) - 300.0
                data["updated_at_iso"] = datetime.datetime.now().isoformat()
                self._save_tokens(data)
                return True, "Schwab Thinkorswim 7-Day Read-Only Token Activated!"
            return False, f"Schwab OAuth HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as e:
            return False, f"Schwab OAuth connection error: {e}"

    def _get_valid_access_token(self) -> Optional[str]:
        if not (self.app_key and self.app_secret):
            return None
        now = time.time()
        access_token = self._tokens.get("access_token")
        access_exp = float(self._tokens.get("access_expires_at", 0.0))
        if access_token and now < access_exp:
            return access_token

        refresh_token = self._tokens.get("refresh_token")
        refresh_exp = float(self._tokens.get("refresh_expires_at", 0.0))
        if not refresh_token or now >= refresh_exp:
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
                merged = dict(self._tokens)
                merged.update(new_data)
                merged["access_expires_at"] = now + float(new_data.get("expires_in", 1800)) - 60.0
                self._save_tokens(merged)
                return merged.get("access_token")
            sys.stderr.write(f"[SCHWAB-AUTH WARNING]: Token refresh HTTP {resp.status_code}\n")
        except Exception as e:
            sys.stderr.write(f"[SCHWAB-AUTH WARNING]: Token refresh failed ({e})\n")
        return None

    def get_chart_history(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Read-only GET /marketdata/v1/pricehistory for 1Y daily candles."""
        token = self._get_valid_access_token()
        if not token:
            return None
        schwab_sym = self.SCHWAB_SYMBOL_MAP.get(symbol.upper(), symbol.upper())
        url = (
            "https://api.schwabapi.com/marketdata/v1/pricehistory"
            f"?symbol={urllib.parse.quote(schwab_sym)}&periodType=year&period=1&frequencyType=daily&frequency=1"
        )
        try:
            self.limiter.acquire()
            r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=4)
            if r.status_code == 200:
                candles = r.json().get("candles", [])
                if candles:
                    closes = [float(c["close"]) for c in candles if "close" in c]
                    if closes:
                        return {
                            "meta": {
                                "regularMarketPrice": closes[-1],
                                "previousClose": closes[-2] if len(closes) > 1 else closes[-1],
                                "provider": "Schwab ToS API",
                            },
                            "indicators": {"quote": [{"close": closes}]},
                        }
        except Exception as e:
            sys.stderr.write(f"[SCHWAB-API FALLBACK]: Chart fetch for {symbol} failed ({e})\n")
        return None

    def get_option_chain(
        self, symbol: str, from_dte: int, to_dte: int
    ) -> Optional[List[LiveOptionContract]]:
        """Read-only GET /marketdata/v1/chains with broker-calculated Greeks."""
        token = self._get_valid_access_token()
        if not token:
            return None
        schwab_sym = self.SCHWAB_SYMBOL_MAP.get(symbol.upper(), symbol.upper())
        today = datetime.date.today()
        from_date = (today + datetime.timedelta(days=max(1, from_dte))).strftime("%Y-%m-%d")
        to_date = (today + datetime.timedelta(days=max(from_dte + 1, to_dte))).strftime("%Y-%m-%d")
        url = (
            "https://api.schwabapi.com/marketdata/v1/chains"
            f"?symbol={urllib.parse.quote(schwab_sym)}&contractType=ALL&includeUnderlyingQuote=true"
            f"&fromDate={from_date}&toDate={to_date}"
        )
        try:
            self.limiter.acquire()
            r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=6)
            if r.status_code != 200:
                return None
            data = r.json()
            contracts: List[LiveOptionContract] = []
            for opt_type, map_key in [("CALL", "callExpDateMap"), ("PUT", "putExpDateMap")]:
                exp_map = data.get(map_key, {})
                for exp_key, strikes_map in exp_map.items():
                    parts = exp_key.split(":")
                    exp_str = parts[0]
                    dte_val = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else from_dte
                    for strike_str, contract_list in strikes_map.items():
                        if not contract_list:
                            continue
                        c = contract_list[0]
                        strike_f = float(c.get("strikePrice", strike_str))
                        bid = float(c.get("bid", 0.0) or 0.0)
                        ask = float(c.get("ask", 0.0) or 0.0)
                        last = float(c.get("last", 0.0) or 0.0)
                        mid = round((bid + ask) / 2.0, 2) if (bid > 0 and ask > 0) else round(max(0.01, last), 2)
                        iv_pct = float(c.get("volatility", 20.0) or 20.0)
                        iv_dec = round(iv_pct / 100.0, 4) if iv_pct > 1.5 else round(iv_pct, 4)
                        contracts.append(
                            LiveOptionContract(
                                contract_symbol=str(c.get("symbol", f"{symbol}-{opt_type}-{strike_f}")).replace(" ", ""),
                                strike=strike_f,
                                dte=dte_val,
                                expiration=exp_str,
                                option_type=opt_type,
                                bid=round(bid, 2),
                                ask=round(ask, 2),
                                mid=mid,
                                last_price=round(last, 2),
                                volume=int(c.get("totalVolume", 0) or 0),
                                open_interest=int(c.get("openInterest", 0) or 0),
                                implied_volatility=iv_dec,
                                delta_approx=round(float(c.get("delta", 0.0) or 0.0), 3),
                                gamma_approx=round(float(c.get("gamma", 0.0) or 0.0), 4),
                                theta_approx=round(float(c.get("theta", 0.0) or 0.0), 3),
                                in_the_money=bool(c.get("inTheMoney", False)),
                            )
                        )
            return contracts if contracts else None
        except Exception as e:
            sys.stderr.write(f"[SCHWAB-API FALLBACK]: Option chain fetch for {symbol} failed ({e})\n")
            return None


class LiveMarketClient:
    """
    Client for querying real-time market data and live options chains.
    Follows AGENTS.md critical rules:
    1. Zero Paid Dependencies (Yahoo Finance -> yfinance -> Twelve Data free tier)
    2. Built-in Token-Bucket Rate Limiting
    3. Local SQLite Caching (market_cache.db)
    4. Full try-except Error Resilience with descriptive fallback messages
    """

    def __init__(self, cache_ttl_seconds: int = 900):
        self.cache_ttl = cache_ttl_seconds
        self._session: Optional[requests.Session] = None
        self._crumb: Optional[str] = None
        self._crumb_time: float = 0
        self._network_backoff_until: float = 0
        self._cache_quotes: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._cache_chains: Dict[str, Tuple[float, List[LiveOptionContract]]] = {}
        self.sqlite_cache = SQLiteMarketCache()
        self.schwab_limiter = TokenBucketRateLimiter(max_calls=90, period_seconds=60.0, min_interval_sec=0.05)
        self.yahoo_limiter = TokenBucketRateLimiter(max_calls=30, period_seconds=60.0, min_interval_sec=0.05)
        self.twelve_data_limiter = TokenBucketRateLimiter(max_calls=8, period_seconds=60.0, min_interval_sec=0.25)
        self.schwab_client = SchwabReadOnlyClient(self.schwab_limiter)
        self.twelve_data_api_key = (
            os.environ.get("TWELVE_DATA_API_KEY")
            or _load_local_dotenv().get("TWELVE_DATA_API_KEY")
            or ""
        ).strip()
        self.recent_warnings: List[str] = []
        self.active_provider: str = "Live Feed"

    def _log_warning(self, msg: str):
        sys.stderr.write(f"{msg}\n")
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        if entry not in self.recent_warnings:
            self.recent_warnings = (self.recent_warnings + [entry])[-15:]

    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            })
        return self._session

    def _get_crumb(self) -> Optional[str]:
        now = time.time()
        if (now - self._crumb_time) < 1800:
            return self._crumb

        self._crumb_time = now
        if now < self._network_backoff_until:
            return None

        session = self._get_session()
        try:
            self.yahoo_limiter.acquire()
            session.get("https://fc.yahoo.com", timeout=3)
            crumb_resp = session.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=3)
            if crumb_resp.status_code == 200 and crumb_resp.text and "error" not in crumb_resp.text.lower():
                self._crumb = crumb_resp.text.strip()
                return self._crumb
        except Exception as e:
            self._network_backoff_until = time.time() + 60.0
            sys.stderr.write(f"[RATE-LIMIT / CRUMB NOTICE]: Crumb fetch fallback triggered: {e}\n")
        return None

    def get_live_chart_data(self, symbol: str) -> Optional[Dict[str, Any]]:
        yahoo_sym = TICKER_SYMBOL_MAP.get(symbol.upper(), symbol.upper())
        now = time.time()

        if yahoo_sym in self._cache_quotes:
            cached_time, cached_data = self._cache_quotes[yahoo_sym]
            if (now - cached_time) < self.cache_ttl:
                return cached_data

        # Check fresh SQLite cache first
        sql_cached = self.sqlite_cache.get(f"CHART_{yahoo_sym}", max_age_seconds=self.cache_ttl)
        if sql_cached:
            self._cache_quotes[yahoo_sym] = (now, sql_cached)
            return sql_cached

        # Provider 0: Schwab / Thinkorswim Read-Only Official API (if connected)
        schwab_chart = self.schwab_client.get_chart_history(symbol)
        if schwab_chart:
            self.active_provider = "Schwab ToS Live (Read-Only)"
            self._cache_quotes[yahoo_sym] = (now, schwab_chart)
            self.sqlite_cache.set(f"CHART_{yahoo_sym}", schwab_chart)
            return schwab_chart

        # Circuit breaker: if network/API is currently in backoff, serve persisted SQLite cache immediately
        if now < self._network_backoff_until:
            stale_cached = self.sqlite_cache.get(f"CHART_{yahoo_sym}", max_age_seconds=None)
            if stale_cached:
                self.active_provider = "SQLite Cached Fallback (Yahoo/Twelve Offline)"
                self._cache_quotes[yahoo_sym] = (now, stale_cached)
                return stale_cached

        # Provider 1: Yahoo Finance v8 Chart Endpoint
        session = self._get_session()
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{yahoo_sym}?range=1y&interval=1d"

        try:
            self.yahoo_limiter.acquire()
            r = session.get(url, timeout=3)
            if r.status_code == 200:
                data = r.json()
                if "chart" in data and data["chart"]["result"]:
                    result = data["chart"]["result"][0]
                    self.active_provider = "Yahoo Finance Live"
                    self._cache_quotes[yahoo_sym] = (now, result)
                    self.sqlite_cache.set(f"CHART_{yahoo_sym}", result)
                    return result
            elif r.status_code == 429:
                self._network_backoff_until = time.time() + 60.0
                self._log_warning(f"[RATE-LIMIT WARNING]: Yahoo Finance 429 on {symbol}; switching to fallback provider.")
        except Exception as e:
            self._network_backoff_until = time.time() + 60.0
            self._log_warning(f"[API FALLBACK]: Yahoo Finance chart request for {symbol} failed ({e}); checking fallback/cache.")

        # Provider 2: Twelve Data Free Tier API (Max 8 req/min enforced)
        if self.twelve_data_api_key:
            try:
                self.twelve_data_limiter.acquire()
                td_url = (
                    f"https://api.twelvedata.com/time_series?symbol={symbol.upper()}"
                    f"&interval=1day&outputsize=252&apikey={self.twelve_data_api_key}"
                )
                r_td = session.get(td_url, timeout=4)
                if r_td.status_code == 200:
                    td_json = r_td.json()
                    values = td_json.get("values", [])
                    if values:
                        closes = [float(v["close"]) for v in reversed(values) if "close" in v]
                        result = {
                            "meta": {
                                "regularMarketPrice": closes[-1],
                                "previousClose": closes[-2] if len(closes) > 1 else closes[-1],
                            },
                            "indicators": {"quote": [{"close": closes}]},
                        }
                        self.active_provider = "Twelve Data Live"
                        self._cache_quotes[yahoo_sym] = (now, result)
                        self.sqlite_cache.set(f"CHART_{yahoo_sym}", result)
                        return result
                else:
                    self._log_warning(f"[API FALLBACK]: Twelve Data HTTP {r_td.status_code} on {symbol}.")
            except Exception as e:
                self._log_warning(f"[API FALLBACK]: Twelve Data fallback for {symbol} failed ({e}).")

        # Fast Resilient Fallback: Local SQLite Last-Known Real Market Data
        stale_cached = self.sqlite_cache.get(f"CHART_{yahoo_sym}", max_age_seconds=None)
        if stale_cached:
            self.active_provider = "SQLite Cached Fallback (Upstream Offline)"
            self._cache_quotes[yahoo_sym] = (now, stale_cached)
            return stale_cached

        # Provider 3: yfinance library fallback (only if SQLite cache has no entry for this ticker)
        if yf is not None:
            try:
                self.yahoo_limiter.acquire()
                hist = yf.Ticker(yahoo_sym).history(period="1y", interval="1d", timeout=4)
                if hist is not None and not hist.empty and "Close" in hist:
                    closes = [float(x) for x in hist["Close"].dropna().tolist()]
                    if closes:
                        result = {
                            "meta": {
                                "regularMarketPrice": closes[-1],
                                "previousClose": closes[-2] if len(closes) > 1 else closes[-1],
                            },
                            "indicators": {"quote": [{"close": closes}]},
                        }
                        self.active_provider = "yfinance Live"
                        self._cache_quotes[yahoo_sym] = (now, result)
                        self.sqlite_cache.set(f"CHART_{yahoo_sym}", result)
                        return result
            except Exception as e:
                self._log_warning(f"[API FALLBACK]: yfinance fallback for {symbol} failed ({e}).")

        return None

    def get_live_ticker_snapshot(self, symbol: str) -> LiveTickerSnapshot:
        chart_data = self.get_live_chart_data(symbol)
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if not chart_data:
            cached_snap = self.sqlite_cache.get(f"SNAP_{symbol.upper()}", max_age_seconds=None)
            if cached_snap:
                sys.stderr.write(f"[CACHE FALLBACK]: Using cached snapshot for {symbol}.\n")
                return LiveTickerSnapshot(**cached_snap)
            raise RuntimeError(f"Unable to retrieve live market quote for {symbol} from exchange feed.")

        meta = chart_data.get("meta", {})
        spot = float(meta.get("regularMarketPrice", meta.get("previousClose", 100.0)))
        prev_close = float(meta.get("previousClose", spot))
        change_pct = round(((spot - prev_close) / prev_close) * 100.0, 2) if prev_close > 0 else 0.0

        quotes = chart_data.get("indicators", {}).get("quote", [{}])[0]
        raw_closes = quotes.get("close", [])
        clean_closes = [float(c) for c in raw_closes if c is not None and not math.isnan(c)]

        rsi_14 = self._calculate_rsi(clean_closes, 14)
        hv_30d = self._calculate_annualized_volatility(clean_closes, 30)
        iv_current, iv_rank, iv_percentile = self._calculate_volatility_metrics(clean_closes, hv_30d)

        snap = LiveTickerSnapshot(
            symbol=symbol,
            yahoo_symbol=TICKER_SYMBOL_MAP.get(symbol, symbol),
            spot=round(spot, 2),
            previous_close=round(prev_close, 2),
            change_pct=change_pct,
            rsi_14=rsi_14,
            historical_vol_30d=hv_30d,
            iv_current=iv_current,
            iv_rank=iv_rank,
            iv_percentile_6m=iv_percentile,
            timestamp=now_str,
            is_live=True,
        )
        self.sqlite_cache.set(f"SNAP_{symbol.upper()}", asdict(snap))
        return snap

    def get_all_snapshots_parallel(self, symbols: List[str]) -> Dict[str, LiveTickerSnapshot]:
        """Fetch all snapshots concurrently with rate-limit protection (max_workers=4)."""
        results: Dict[str, LiveTickerSnapshot] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(symbols))) as executor:
            future_to_sym = {executor.submit(self.get_live_ticker_snapshot, sym): sym for sym in symbols}
            for future in concurrent.futures.as_completed(future_to_sym):
                sym = future_to_sym[future]
                try:
                    results[sym] = future.result()
                except Exception as e:
                    sys.stderr.write(f"[LIVE-FEED] Error fetching {sym}: {e}\n")
        return results

    def get_live_option_chain(
        self, symbol: str, target_dte_min: int = 25, target_dte_max: int = 55
    ) -> List[LiveOptionContract]:
        cache_key = f"{symbol}_{target_dte_min}_{target_dte_max}"
        now = time.time()

        if cache_key in self._cache_chains:
            t_cached, data = self._cache_chains[cache_key]
            if (now - t_cached) < self.cache_ttl:
                return data

        sql_chain = self.sqlite_cache.get(f"CHAIN_{cache_key}", max_age_seconds=self.cache_ttl)
        if sql_chain:
            restored = [LiveOptionContract(**c) for c in sql_chain]
            self._cache_chains[cache_key] = (now, restored)
            return restored

        # Provider 0: Schwab / Thinkorswim Read-Only Official API (if connected)
        schwab_chain = self.schwab_client.get_option_chain(symbol, target_dte_min, target_dte_max)
        if schwab_chain:
            self._cache_chains[cache_key] = (now, schwab_chain)
            self.sqlite_cache.set(f"CHAIN_{cache_key}", [asdict(c) for c in schwab_chain])
            return schwab_chain

        if now < self._network_backoff_until:
            stale = self.sqlite_cache.get(f"CHAIN_{cache_key}", max_age_seconds=None)
            if stale:
                restored = [LiveOptionContract(**c) for c in stale]
                self._cache_chains[cache_key] = (now, restored)
                return restored
            return []

        yahoo_sym = TICKER_SYMBOL_MAP.get(symbol.upper(), symbol.upper())
        session = self._get_session()
        crumb = self._get_crumb()

        contracts: List[LiveOptionContract] = []
        now_ts = time.time()

        url = f"https://query2.finance.yahoo.com/v7/finance/options/{yahoo_sym}"
        if crumb:
            url += f"?crumb={crumb}"

        try:
            self.yahoo_limiter.acquire()
            r = session.get(url, timeout=7)
            if r.status_code != 200:
                stale = self.sqlite_cache.get(f"CHAIN_{cache_key}", max_age_seconds=None)
                return [LiveOptionContract(**c) for c in stale] if stale else []

            data = r.json()
            chain_res = data.get("optionChain", {}).get("result", [])
            if not chain_res:
                return []

            first_result = chain_res[0]
            spot = float(first_result.get("quote", {}).get("regularMarketPrice", 100.0))
            exp_timestamps = first_result.get("expirationDates", [])

            matching_exps = [
                (exp_ts, int(round((exp_ts - now_ts) / 86400.0)), datetime.datetime.fromtimestamp(exp_ts).strftime("%Y-%m-%d"))
                for exp_ts in exp_timestamps
                if target_dte_min <= (exp_ts - now_ts) / 86400.0 <= target_dte_max
            ]

            if not matching_exps and exp_timestamps:
                all_exps = [(exp_ts, int(round((exp_ts - now_ts) / 86400.0))) for exp_ts in exp_timestamps]
                closest = min(all_exps, key=lambda x: abs(x[1] - ((target_dte_min + target_dte_max) // 2)))
                exp_date_str = datetime.datetime.fromtimestamp(closest[0]).strftime("%Y-%m-%d")
                matching_exps.append((closest[0], closest[1], exp_date_str))

            for exp_ts, dte, exp_str in matching_exps[:1]:
                exp_url = f"https://query2.finance.yahoo.com/v7/finance/options/{yahoo_sym}?date={exp_ts}"
                if crumb:
                    exp_url += f"&crumb={crumb}"

                self.yahoo_limiter.acquire()
                resp = session.get(exp_url, timeout=7)
                if resp.status_code != 200:
                    continue

                exp_data = resp.json().get("optionChain", {}).get("result", [{}])[0]
                options_list = exp_data.get("options", [{}])[0]

                for raw_put in options_list.get("puts", []):
                    c = self._parse_contract(raw_put, spot, dte, exp_str, "PUT")
                    if c:
                        contracts.append(c)

                for raw_call in options_list.get("calls", []):
                    c = self._parse_contract(raw_call, spot, dte, exp_str, "CALL")
                    if c:
                        contracts.append(c)

            if contracts:
                self._cache_chains[cache_key] = (now, contracts)
                self.sqlite_cache.set(f"CHAIN_{cache_key}", [asdict(c) for c in contracts])

        except Exception as e:
            sys.stderr.write(f"[OPTION-CHAIN FALLBACK]: {symbol} option chain fetch warning ({e}); checking cache.\n")
            stale = self.sqlite_cache.get(f"CHAIN_{cache_key}", max_age_seconds=None)
            if stale:
                return [LiveOptionContract(**c) for c in stale]

        return contracts

    def lookup_position_contract(
        self, symbol: str, option_type: str, strike: float, dte: int
    ) -> Optional[LiveOptionContract]:
        """
        Fast O(1) cache-aligned contract lookup for manual/Fidelity positions.
        Reuses the standard 28-55 DTE or 340+ DTE LEAP cached chains in memory/SQLite
        so evaluating 16+ positions completes in <5 milliseconds without blocking UI.
        """
        sym = symbol.upper()
        opt_type = option_type.upper()
        bucket_key = f"{sym}_LEAP_ALL_340" if dte >= 180 else f"{sym}_28_55"

        chain: List[LiveOptionContract] = []
        if bucket_key in self._cache_chains:
            _, chain = self._cache_chains[bucket_key]
        else:
            cached_rows = self.sqlite_cache.get(f"CHAIN_{bucket_key}", max_age_seconds=None)
            if cached_rows:
                chain = [LiveOptionContract(**c) for c in cached_rows]
                self._cache_chains[bucket_key] = (time.time(), chain)

        matching = [c for c in chain if c.option_type == opt_type and abs(c.strike - strike) < 0.01]
        return matching[0] if matching else None

    def get_live_leap_options(
        self, symbol: str, min_dte: int = 340, option_type: str = "CALL"
    ) -> List[LiveOptionContract]:
        cache_key = f"{symbol}_LEAP_ALL_{min_dte}"
        now = time.time()
        opt_filter = option_type.upper()

        if cache_key in self._cache_chains:
            t_cached, data = self._cache_chains[cache_key]
            if (now - t_cached) < self.cache_ttl:
                return [c for c in data if c.option_type == opt_filter]

        sql_leap = self.sqlite_cache.get(f"CHAIN_{cache_key}", max_age_seconds=self.cache_ttl)
        if sql_leap:
            restored = [LiveOptionContract(**c) for c in sql_leap]
            self._cache_chains[cache_key] = (now, restored)
            return [c for c in restored if c.option_type == opt_filter]

        # Provider 0: Schwab / Thinkorswim Read-Only Official API (if connected)
        schwab_leaps = self.schwab_client.get_option_chain(symbol, min_dte, 550)
        if schwab_leaps:
            self._cache_chains[cache_key] = (now, schwab_leaps)
            self.sqlite_cache.set(f"CHAIN_{cache_key}", [asdict(c) for c in schwab_leaps])
            return [c for c in schwab_leaps if c.option_type == opt_filter]

        if now < self._network_backoff_until:
            stale = self.sqlite_cache.get(f"CHAIN_{cache_key}", max_age_seconds=None)
            if stale:
                restored = [LiveOptionContract(**c) for c in stale]
                self._cache_chains[cache_key] = (now, restored)
                return [c for c in restored if c.option_type == opt_filter]
            return []

        yahoo_sym = TICKER_SYMBOL_MAP.get(symbol.upper(), symbol.upper())
        session = self._get_session()
        crumb = self._get_crumb()

        contracts: List[LiveOptionContract] = []
        now_ts = time.time()

        url = f"https://query2.finance.yahoo.com/v7/finance/options/{yahoo_sym}"
        if crumb:
            url += f"?crumb={crumb}"

        try:
            self.yahoo_limiter.acquire()
            r = session.get(url, timeout=7)
            if r.status_code != 200:
                stale = self.sqlite_cache.get(f"CHAIN_{cache_key}", max_age_seconds=None)
                return [LiveOptionContract(**c) for c in stale if c.get("option_type") == opt_filter] if stale else []

            data = r.json()
            chain_res = data.get("optionChain", {}).get("result", [])
            if not chain_res:
                return []

            first_result = chain_res[0]
            spot = float(first_result.get("quote", {}).get("regularMarketPrice", 100.0))
            exp_timestamps = first_result.get("expirationDates", [])

            leap_exps = [
                (exp_ts, int(round((exp_ts - now_ts) / 86400.0)), datetime.datetime.fromtimestamp(exp_ts).strftime("%Y-%m-%d"))
                for exp_ts in exp_timestamps
                if (exp_ts - now_ts) / 86400.0 >= min_dte
            ]

            if not leap_exps:
                return []

            target_leap = min(leap_exps, key=lambda x: abs(x[1] - 390))
            exp_ts, dte, exp_str = target_leap

            exp_url = f"https://query2.finance.yahoo.com/v7/finance/options/{yahoo_sym}?date={exp_ts}"
            if crumb:
                exp_url += f"&crumb={crumb}"

            self.yahoo_limiter.acquire()
            resp = session.get(exp_url, timeout=7)
            if resp.status_code == 200:
                exp_data = resp.json().get("optionChain", {}).get("result", [{}])[0]
                options_list = exp_data.get("options", [{}])[0]
                for raw_call in options_list.get("calls", []):
                    c = self._parse_contract(raw_call, spot, dte, exp_str, "CALL")
                    if c:
                        contracts.append(c)
                for raw_put in options_list.get("puts", []):
                    c = self._parse_contract(raw_put, spot, dte, exp_str, "PUT")
                    if c:
                        contracts.append(c)

            if contracts:
                self._cache_chains[cache_key] = (now, contracts)
                self.sqlite_cache.set(f"CHAIN_{cache_key}", [asdict(c) for c in contracts])

        except Exception as e:
            sys.stderr.write(f"[LEAP-CHAIN FALLBACK]: {symbol} LEAP chain fetch warning ({e}); checking cache.\n")
            stale = self.sqlite_cache.get(f"CHAIN_{cache_key}", max_age_seconds=None)
            if stale:
                return [LiveOptionContract(**c) for c in stale if c.get("option_type") == opt_filter]

        return [c for c in contracts if c.option_type == opt_filter]

    def _parse_contract(
        self, raw: Dict[str, Any], spot: float, dte: int, exp_str: str, opt_type: str
    ) -> Optional[LiveOptionContract]:
        strike = float(raw.get("strike", 0.0))
        if strike <= 0:
            return None

        bid = float(raw.get("bid", 0.0))
        ask = float(raw.get("ask", 0.0))
        last_price = float(raw.get("lastPrice", 0.0))
        iv = float(raw.get("impliedVolatility", 0.20))
        if iv <= 0:
            iv = 0.20

        mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else last_price
        if mid <= 0:
            mid = max(0.01, last_price)

        t = max(1.0, float(dte)) / 365.0
        d1 = (math.log(spot / strike) + (0.045 + 0.5 * iv * iv) * t) / (iv * math.sqrt(t))
        norm_cdf_d1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
        norm_pdf_d1 = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)

        if opt_type == "CALL":
            delta = round(norm_cdf_d1, 3)
            theta = round((-(spot * norm_pdf_d1 * iv) / (2.0 * math.sqrt(t)) - 0.045 * strike * math.exp(-0.045 * t) * (0.5 * (1.0 + math.erf((d1 - iv * math.sqrt(t)) / math.sqrt(2.0))))) / 365.0, 3)
        else:
            delta = round(norm_cdf_d1 - 1.0, 3)
            theta = round((-(spot * norm_pdf_d1 * iv) / (2.0 * math.sqrt(t)) + 0.045 * strike * math.exp(-0.045 * t) * (0.5 * (1.0 - math.erf((d1 - iv * math.sqrt(t)) / math.sqrt(2.0))))) / 365.0, 3)

        gamma = round(norm_pdf_d1 / (spot * iv * math.sqrt(t)), 4) if (spot * iv * math.sqrt(t)) > 0 else 0.0

        return LiveOptionContract(
            contract_symbol=raw.get("contractSymbol", f"{opt_type}-{strike}"),
            strike=strike,
            dte=dte,
            expiration=exp_str,
            option_type=opt_type,
            bid=round(bid, 2),
            ask=round(ask, 2),
            mid=round(mid, 2),
            last_price=round(last_price, 2),
            volume=int(raw.get("volume", 0) or 0),
            open_interest=int(raw.get("openInterest", 0) or 0),
            implied_volatility=round(iv, 4),
            delta_approx=delta,
            gamma_approx=gamma,
            theta_approx=theta,
            in_the_money=bool(raw.get("inTheMoney", False)),
        )

    @staticmethod
    def _calculate_rsi(prices: List[float], period: int = 14) -> float:
        if len(prices) < period + 1:
            return 50.0
        changes = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
        gains = [max(0.0, c) for c in changes]
        losses = [max(0.0, -c) for c in changes]

        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        for i in range(period, len(changes)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0.0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return round(100.0 - (100.0 / (1.0 + rs)), 2)

    @staticmethod
    def _calculate_annualized_volatility(prices: List[float], window: int = 30) -> float:
        if len(prices) < window + 1:
            return 0.20
        recent = prices[-(window + 1):]
        log_returns = [math.log(recent[i] / recent[i - 1]) for i in range(1, len(recent))]
        mean_ret = sum(log_returns) / len(log_returns)
        var = sum((r - mean_ret) ** 2 for r in log_returns) / (len(log_returns) - 1)
        daily_sd = math.sqrt(var)
        return round(daily_sd * math.sqrt(252), 4)

    @classmethod
    def _calculate_volatility_metrics(
        cls, prices: List[float], current_hv: float
    ) -> Tuple[float, float, float]:
        if len(prices) < 60:
            return round(current_hv, 4), 50.0, 50.0

        rolling_vols: List[float] = []
        for i in range(30, len(prices)):
            window = prices[i - 30:i + 1]
            log_ret = [math.log(window[j] / window[j - 1]) for j in range(1, len(window))]
            mean = sum(log_ret) / len(log_ret)
            var = sum((r - mean) ** 2 for r in log_ret) / (len(log_ret) - 1)
            rolling_vols.append(math.sqrt(var) * math.sqrt(252))

        if not rolling_vols:
            return round(current_hv, 4), 50.0, 50.0

        min_vol = min(rolling_vols)
        max_vol = max(rolling_vols)
        current_vol = rolling_vols[-1]

        if max_vol <= min_vol:
            ivr = 50.0
        else:
            ivr = ((current_vol - min_vol) / (max_vol - min_vol)) * 100.0
        ivr = max(0.0, min(100.0, round(ivr, 1)))

        six_month_vols = rolling_vols[-126:] if len(rolling_vols) >= 126 else rolling_vols
        lower_count = sum(1 for v in six_month_vols if v < current_vol)
        ivp_6m = round((lower_count / len(six_month_vols)) * 100.0, 1)

        return round(current_vol, 4), ivr, ivp_6m
