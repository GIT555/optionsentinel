# Multi-Broker Option Sentinel & Quantitative Signal Action Engine (Live Market Edition)

An institutional quantitative options scanner, strike selector, and automated signal capture engine operating inside the **Antigravity SDK** environment.

Aggregates **live market quotes**, **1-year daily candles**, and **real exchange option chains** across the operational 12-ticker multi-asset matrix with native Windows certificate verification.

---

## 1. Operational Watchlist Matrix

| Category | Tickers | Characteristics & Live Quotes |
| :--- | :--- | :--- |
| **Core Equity / Tax-Advantaged** | `SPY`, `QQQ`, `XSP`, `QQQM`, `SPYM` | `XSP` offers **Section 1256 (60/40)** long/short term capital gains tax treatment with European cash-settlement. |
| **Triple-Leveraged Equities** | `TQQQ`, `SPXL` | 3x daily leverage. High theta decay & volatility drag. Subject to conservative **0.20 Delta** constraints and strictly excluded from LEAPs. |
| **Macro Commodities** | `GLD`, `SLV`, `IAU` | Sovereign macro hedges (physical gold & silver bullion). Lower correlation to equity beta. |
| **Spot Crypto & Bonds** | `IBIT`, `TLT` | `IBIT` (Spot Bitcoin ETF) represents high-volatility crypto beta. `TLT` (20+ Year Treasury) represents duration interest rate sensitivity. |

---

## 2. Quantitative Architecture & Mathematical Directives

### A. Premium Entry Engine (The Wheel CSP)
- **Trigger Conditions**:
  $$\text{IV Rank} > 40\% \quad \text{OR} \quad \text{6-Month IV Percentile} > 50\text{th percentile}$$
- **1-Standard Deviation Statistical Strike Selection**:
  $$K_{\text{CSP}} = S \cdot \left(1.0 - 1.0 \times \sigma \times \sqrt{\frac{45}{365}}\right)$$
  - Snaps to real, live exchange option contracts.
  - Automatically targets **0.30 Delta** for standard equity/commodities, and constrained to **0.20 Delta** for leveraged assets (`TQQQ`, `SPXL`, `IBIT`).
- **Target Maturity**: 30 to 45 Days to Expiration (DTE).
- **Exit Directives**: Pre-generates exit orders at **50% of maximum credit** received.
- **Roll Directive**: Structural roll alert triggered whenever DTE hits **14 Days**.

### B. Leverage Entry Engine (LEAP Long Call & PMCC)
- **Trigger Conditions**:
  $$\text{IV Rank} < 20\% \quad \text{AND} \quad \text{14-Day Wilder RSI} < 35$$
- **Automatic Leveraged ETF Exclusion**:
  - Leveraged assets (`TQQQ`, `SPXL`) are strictly suppressed and flagged as `REJECT`.
  - Eliminates geometric beta-slippage, path-dependent compounding drag, and volatility erosion over 360+ day holding periods.
- **Deep ITM Long Leg**:
  - Target: **0.85 Delta Call** expiring **360+ days out** (e.g., 340-450 DTE).
  - Binds to live exchange LEAP contracts (e.g. `GLD270930C00270000`, `SLV270930C00040000`, `IAU280121C00052000`).
- **Poor Man's Covered Call (PMCC) Overlay**:
  - Sells short-term **0.30 Delta Calls** at 30 to 45 DTE against the long LEAP diagonal to generate continuous cash-flow yield and lower net debit.

### C. Dynamic Exit Engine & Real-Time Gamma Danger Zone Sentinel
- **Rule 1 (Gamma Danger Zone)**:
  $$\text{DTE} < 14$$
  - As expiration approaches near the money, Gamma ($\Gamma = \frac{\partial^2 V}{\partial S^2}$) accelerates exponentially toward infinity.
  - Automatically emits real-time timestamped diagnostic alerts to `sys.stderr` and formats structural roll procedures (roll out to 42 DTE).
- **Rule 2 (50% Profit Contraction)**:
  $$\frac{P_{\text{entry}} - P_{\text{current}}}{P_{\text{entry}}} \ge 50\%$$
  - Emits Buy-To-Close (BTC) orders to harvest alpha, eliminate remaining tail risk, and free up buying power.
- **Dual Trigger**: If both conditions are met, prioritizes immediate BTC closure.

---

## 3. Scripts & Orchestration

The repository contains three production quantitative engines:

### 1. `live_market_data.py`
High-performance live exchange client:
- Injects native Windows Certificate Store using `truststore` for firewall/antivirus compatibility.
- Fetches real-time spot quotes, 252-day historical candles, and live option chains (calls & puts across all expiries).
- In-memory caching with 45-second TTL.

### 2. `signal_sentinel.py`
The definitive quantitative signal capture and action engine:
- Computes exact 1-Standard Deviation expected moves ($\pm 1\sigma$).
- Binds to actual live exchange option contracts with real Bid, Ask, Volume, and Open Interest.
- Automates leveraged ETF suppression with clear rejection rationales.
- Evaluates active position books against 14 DTE and 50% profit thresholds.

```bash
# Run live institutional signal audit
python signal_sentinel.py --scan

# Run in offline simulation fallback mode
python signal_sentinel.py --mock

# Export live telemetry & signals as JSON
python signal_sentinel.py --json
```

### 3. `option_sentinel.py`
The full analytical platform featuring:
- Live option chains for strike selection.
- Background worker monitoring active positions and emitting real-time `sys.stderr` alerts.
- Built-in multi-threaded HTTP server hosting the dark-mode analytical UI dashboard.

```bash
# Launch interactive live UI dashboard (default port 8765)
python option_sentinel.py --server --port 8765

# Run terminal scan mode
python option_sentinel.py --cli
```

---

## 4. Verification & Test Suite

Run the full automated test suite covering Black-Scholes inversion, 1-SD formulas, strategy filters, leveraged exclusions, and exit triggers:

```bash
python test_sentinels.py
```
