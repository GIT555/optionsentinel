"""
Schwab Persistent Automated Browser Auth (schwab_headless_auth.py)
==================================================================
Uses a persistent Microsoft Edge / Chromium profile so Schwab remembers your device.
- Direct URL interception: Intercepts https://127.0.0.1/?code=... directly in memory.
- No local web server needed, no certificate warnings, no port 443 conflicts.
- First time: Browser opens, you log in once and check "Trust this device".
- Subsequent runs: Automatically handles authentication and captures 7-day token.
"""

from __future__ import annotations

import os
import sys
import time
import urllib.parse
from typing import Optional, Tuple

from ..market.client import SchwabReadOnlyClient, TokenBucketRateLimiter
from ..config import DATA_DIR


PROFILE_DIR = os.path.join(DATA_DIR, ".schwab_browser_profile")


def run_schwab_browser_auth(headless: bool = False, timeout_sec: int = 180) -> Tuple[bool, str]:
    """
    Launches persistent Edge browser context, navigates to Schwab auth,
    and intercepts the redirect URL containing ?code=...
    """
    import truststore
    truststore.inject_into_ssl()

    from playwright.sync_api import sync_playwright

    sc = SchwabReadOnlyClient(TokenBucketRateLimiter(8, 60))
    auth_url = sc.get_auth_url()
    if not auth_url:
        return False, "Missing SCHWAB_APP_KEY / SCHWAB_APP_SECRET in environment or .env."

    os.makedirs(PROFILE_DIR, exist_ok=True)

    print("\n=======================================================")
    print("   AUTOMATED SCHWAB TOKEN AUTHENTICATION (PLAYWRIGHT)  ")
    print("=======================================================")
    print(f"[*] Profile Directory: {PROFILE_DIR}")
    print("[*] Launching browser...")

    captured_code: list[str] = []

    with sync_playwright() as p:
        browser_channel = "msedge"
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=PROFILE_DIR,
                channel=browser_channel,
                headless=headless,
                args=[
                    "--ignore-certificate-errors",
                    "--disable-blink-features=AutomationControlled",
                ],
                viewport={"width": 1100, "height": 800},
            )
        except Exception as e:
            # Fallback to standard chromium if msedge channel fails
            try:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=PROFILE_DIR,
                    headless=headless,
                    args=["--ignore-certificate-errors"],
                )
            except Exception as e2:
                return False, f"Could not launch browser: {e2}"

        page = context.pages[0] if context.pages else context.new_page()

        # Direct in-memory route interception on the entire browser context
        def handle_schwab_redirect(route):
            url = route.request.url
            if "code=" in url:
                parsed = urllib.parse.urlparse(url)
                qs = urllib.parse.parse_qs(parsed.query)
                c = qs.get("code", [""])[0]
                if c and c not in captured_code:
                    captured_code.append(c)
            route.fulfill(
                status=200,
                content_type="text/html",
                body="""<!DOCTYPE html><html><body style="background:#0b1329;color:#4ade80;font-family:sans-serif;text-align:center;padding:50px;">
                <h1>&#9989; Schwab Market Data Token Captured!</h1>
                <p style="color:#cbd5e1;">Exchanging token and closing browser...</p>
                </body></html>""",
            )

        context.route("**127.0.0.1**", handle_schwab_redirect)

        # Also hook request events across all pages/tabs
        def on_request(request):
            url = request.url
            if "127.0.0.1" in url and "code=" in url:
                parsed = urllib.parse.urlparse(url)
                qs = urllib.parse.parse_qs(parsed.query)
                c = qs.get("code", [""])[0]
                if c and c not in captured_code:
                    captured_code.append(c)

        context.on("request", on_request)

        print("[*] Navigating to Schwab authorization portal...")
        try:
            page.goto(auth_url, wait_until="commit", timeout=30000)
        except Exception:
            pass

        print("[*] Waiting for authentication and approval...")
        print("    -> Log into Schwab in the opened browser window.")
        print("    -> Check 'Remember my ID' / 'Trust this device' so future logins are automatic.")
        print("    -> Click 'Approve'. As soon as you approve, this script intercepts the code in 0.1s!\n")

        start_time = time.time()
        while time.time() - start_time < timeout_sec:
            # Check if captured via request interception
            if captured_code:
                break

            # Also check page.url directly
            curr_url = page.url
            if "127.0.0.1" in curr_url and "code=" in curr_url:
                parsed = urllib.parse.urlparse(curr_url)
                qs = urllib.parse.parse_qs(parsed.query)
                c = qs.get("code", [""])[0]
                if c:
                    captured_code.append(c)
                    break

            time.sleep(0.5)

        context.close()

    if not captured_code:
        return False, f"Timed out after {timeout_sec}s waiting for Schwab login."

    raw_code = captured_code[0]
    print("[*] Captured authorization code! Exchanging for 7-day token...")

    ok, msg = sc.exchange_code_from_redirect(raw_code)
    return ok, msg


if __name__ == "__main__":
    ok, msg = run_schwab_browser_auth(headless=False)
    if ok:
        print(f"\n\033[92;1m[SUCCESS]: {msg}\033[0m")
        print("Saved to git-ignored schwab_tokens.json.")
        print("Your dashboard is now live with real Schwab options, correct LEAP dates, and broker Greeks!\n")
        sys.exit(0)
    else:
        print(f"\n\033[91;1m[ERROR]: {msg}\033[0m\n")
        sys.exit(1)
