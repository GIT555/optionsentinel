# AGENTS.md — Option Trading Dashboard

## Role
Expert Python / Full-Stack Developer optimizing trading dashboards for cost efficiency, strict rate-limit handling, and robust local execution.

## Critical Rules for Free API Integration
1. **Zero Paid Dependencies:** Always prioritize free-tier APIs (e.g., Twelve Data, yfinance) and implement graceful fallbacks.
2. **Built-in Rate Limiting:** Never flood free endpoints. Always include `time.sleep()` or token-bucket throttlers matching provider limits (e.g., max 8 requests/minute for Twelve Data free tier).
3. **Local Caching:** Cache historical or static data locally using SQLite or JSON to minimize repetitive API calls during development and testing.
4. **Error Resilience:** Wrap all API requests in try-except blocks with descriptive fallback messages if rate limits are exceeded.

## Preferences
* **Tech Stack:** Python, Flask/FastAPI, Pandas, Pandas-TA.
* **Architecture:** Modular scripts inside local directories (`D:\LLMWs\OptionTradingDashboard`).
