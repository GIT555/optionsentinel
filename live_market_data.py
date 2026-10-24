"""
Live Market Data Engine (live_market_data.py) - Parallel Edition
================================================================
Institutional Real-Time Market Data and Option Chain Aggregator.
Fetches live equity spot quotes, historical daily candles (for RSI & volatility),
and real exchange options chains concurrently using ThreadPoolExecutor.

Uses native Windows Certificate Store via truststore for corporate/antivirus compatibility.
"""

from __future__ import annotations

import concurrent.futures
import datetime
import math
import sys
import time
from dataclasses import dataclass
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


class LiveMarketClient:
    """Client for querying real-time market data and live options chains in parallel."""

    def __init__(self, cache_ttl_seconds: int = 60):
        self.cache_ttl = cache_ttl_seconds
        self._session: Optional[requests.Session] = None
        self._crumb: Optional[str] = None
        self._crumb_time: float = 0
        self._cache_quotes: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._cache_chains: Dict[str, Tuple[float, List[LiveOptionContract]]] = {}

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
        if self._crumb and (now - self._crumb_time) < 1800:
            return self._crumb

        session = self._get_session()
        try:
            session.get("https://fc.yahoo.com", timeout=6)
            crumb_resp = session.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=6)
            if crumb_resp.status_code == 200 and crumb_resp.text and "error" not in crumb_resp.text.lower():
                self._crumb = crumb_resp.text.strip()
                self._crumb_time = now
                return self._crumb
        except Exception:
            pass
        return None

    def get_live_chart_data(self, symbol: str) -> Optional[Dict[str, Any]]:
        yahoo_sym = TICKER_SYMBOL_MAP.get(symbol.upper(), symbol.upper())
        now = time.time()

        if yahoo_sym in self._cache_quotes:
            cached_time, cached_data = self._cache_quotes[yahoo_sym]
            if (now - cached_time) < self.cache_ttl:
                return cached_data

        session = self._get_session()
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{yahoo_sym}?range=1y&interval=1d"

        try:
            r = session.get(url, timeout=7)
            if r.status_code == 200:
                data = r.json()
                if "chart" in data and data["chart"]["result"]:
                    result = data["chart"]["result"][0]
                    self._cache_quotes[yahoo_sym] = (now, result)
                    return result
        except Exception:
            pass
        return None

    def get_live_ticker_snapshot(self, symbol: str) -> LiveTickerSnapshot:
        chart_data = self.get_live_chart_data(symbol)
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if not chart_data:
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

        return LiveTickerSnapshot(
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

    def get_all_snapshots_parallel(self, symbols: List[str]) -> Dict[str, LiveTickerSnapshot]:
        """Fetch all snapshots concurrently for ultra-low latency."""
        results: Dict[str, LiveTickerSnapshot] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(12, len(symbols))) as executor:
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

        yahoo_sym = TICKER_SYMBOL_MAP.get(symbol.upper(), symbol.upper())
        session = self._get_session()
        crumb = self._get_crumb()

        contracts: List[LiveOptionContract] = []
        now_ts = time.time()

        url = f"https://query2.finance.yahoo.com/v7/finance/options/{yahoo_sym}"
        if crumb:
            url += f"?crumb={crumb}"

        try:
            r = session.get(url, timeout=7)
            if r.status_code != 200:
                return []

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

            self._cache_chains[cache_key] = (now, contracts)

        except Exception:
            pass

        return contracts

    def get_live_leap_options(self, symbol: str, min_dte: int = 340) -> List[LiveOptionContract]:
        cache_key = f"{symbol}_LEAP_{min_dte}"
        now = time.time()

        if cache_key in self._cache_chains:
            t_cached, data = self._cache_chains[cache_key]
            if (now - t_cached) < self.cache_ttl:
                return data

        yahoo_sym = TICKER_SYMBOL_MAP.get(symbol.upper(), symbol.upper())
        session = self._get_session()
        crumb = self._get_crumb()

        contracts: List[LiveOptionContract] = []
        now_ts = time.time()

        url = f"https://query2.finance.yahoo.com/v7/finance/options/{yahoo_sym}"
        if crumb:
            url += f"?crumb={crumb}"

        try:
            r = session.get(url, timeout=7)
            if r.status_code != 200:
                return []

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

            resp = session.get(exp_url, timeout=7)
            if resp.status_code == 200:
                exp_data = resp.json().get("optionChain", {}).get("result", [{}])[0]
                options_list = exp_data.get("options", [{}])[0]
                for raw_call in options_list.get("calls", []):
                    c = self._parse_contract(raw_call, spot, dte, exp_str, "CALL")
                    if c:
                        contracts.append(c)

            self._cache_chains[cache_key] = (now, contracts)

        except Exception:
            pass

        return contracts

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
