"""
Schwab Persistent Automated Browser Auth (schwab_auth.py)
=========================================================
Uses a persistent Microsoft Edge / Chromium profile so Schwab remembers your device.
- Direct URL interception: Intercepts https://127.0.0.1/?code=... directly in memory.
- No local web server needed, no certificate warnings, no port 443 conflicts.
- First time: Browser opens, you log in once and check "Trust this device".
- Subsequent runs: Automatically handles authentication and captures 7-day token.
- Non-blocking token exchange: Exchanges code immediately upon interception before
  browser teardown to prevent persistent profile closing hangs on Windows.
"""

from __future__ import annotations

import os
import sys
import time
import urllib.parse
from typing import Any, List, Optional, Tuple

from ..market.client import SchwabReadOnlyClient, TokenBucketRateLimiter
from ..config import DATA_DIR


PROFILE_DIR = os.path.join(DATA_DIR, ".schwab_chrome_profile")


def run_schwab_browser_auth(headless: bool = False, timeout_sec: int = 180) -> Tuple[bool, str]:
    """
    Launches persistent Google Chrome browser context, navigates to Schwab auth,
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
    print("   AUTOMATED SCHWAB TOKEN AUTHENTICATION (CHROME)      ")
    print("=======================================================")
    print(f"[*] Profile Directory: {PROFILE_DIR}")
    print("[*] Launching Google Chrome...")

    captured_code: List[str] = []
    exchange_result: List[Any] = [False, "No token captured"]

    with sync_playwright() as p:
        browser_channel = "chrome"
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
            # Fallback to standard chromium if chrome channel fails
            try:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=PROFILE_DIR,
                    headless=headless,
                    args=["--ignore-certificate-errors"],
                )
            except Exception as e2:
                return False, f"Could not launch browser: {e2}"

        page = context.pages[0] if context.pages else context.new_page()

        def do_immediate_exchange(auth_code: str):
            if exchange_result[0]:
                return  # already successfully exchanged
            print(f"\n[*] Intercepted Schwab OAuth authorization code: {auth_code[:15]}...")
            print("[*] Exchanging code immediately with Schwab OAuth API for 7-day token...")
            ok, msg = sc.exchange_code_from_redirect(auth_code)
            exchange_result[0] = ok
            exchange_result[1] = msg
            if ok:
                print(f"\033[92;1m[SUCCESS]: {msg}\033[0m")
                print("[*] 7-day token securely written to data/schwab_tokens.json.")
            else:
                print(f"\033[91;1m[ERROR]: Token exchange failed: {msg}\033[0m")

        # Direct in-memory route interception on the entire browser context
        def handle_schwab_redirect(route):
            url = route.request.url
            if "code=" not in url:
                try:
                    route.continue_()
                except Exception:
                    pass
                return

            parsed = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed.query)
            c = qs.get("code", [""])[0]
            if not c and "#" in url:
                frag = urllib.parse.parse_qs(parsed.fragment)
                c = frag.get("code", [""])[0]

            if not c:
                try:
                    route.continue_()
                except Exception:
                    pass
                return

            if c not in captured_code:
                captured_code.append(c)

            # Perform exchange BEFORE responding so user sees accurate status in browser
            do_immediate_exchange(c)

            if exchange_result[0]:
                body = """<!DOCTYPE html><html><body style="background:#0b1329;color:#4ade80;font-family:sans-serif;text-align:center;padding:50px;">
                <h1>&#9989; Schwab Market Data Token Activated!</h1>
                <p style="color:#f8fafc;font-size:18px;">7-Day Access Token Saved & Verified.</p>
                <p style="color:#94a3b8;font-size:14px;">Option Sentinel is now connected to live Thinkorswim feeds. Closing browser...</p>
                </body></html>"""
            else:
                body = f"""<!DOCTYPE html><html><body style="background:#0b1329;color:#f87171;font-family:sans-serif;text-align:center;padding:50px;">
                <h1>&#10060; Schwab Token Exchange Failed</h1>
                <p style="color:#f8fafc;font-size:16px;">{exchange_result[1]}</p>
                <p style="color:#94a3b8;font-size:14px;">Check console output for details.</p>
                </body></html>"""

            try:
                route.fulfill(
                    status=200,
                    content_type="text/html",
                    body=body,
                )
            except Exception:
                pass

        context.route(lambda url: "127.0.0.1" in url, handle_schwab_redirect)

        # Also hook request events across all pages/tabs
        def on_request(request):
            url = request.url
            if "127.0.0.1" in url and "code=" in url:
                parsed = urllib.parse.urlparse(url)
                qs = urllib.parse.parse_qs(parsed.query)
                c = qs.get("code", [""])[0]
                if c and c not in captured_code:
                    captured_code.append(c)
                    do_immediate_exchange(c)

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
            # Check if captured via request interception or exchange already done
            if exchange_result[0] or captured_code:
                if captured_code and not exchange_result[0]:
                    do_immediate_exchange(captured_code[0])
                break

            # Also check page.url directly
            try:
                curr_url = page.url
                if "127.0.0.1" in curr_url and "code=" in curr_url:
                    parsed = urllib.parse.urlparse(curr_url)
                    qs = urllib.parse.parse_qs(parsed.query)
                    c = qs.get("code", [""])[0]
                    if c:
                        if c not in captured_code:
                            captured_code.append(c)
                        do_immediate_exchange(c)
                        break
            except Exception:
                pass

            time.sleep(0.3)

        # Allow browser to display the green confirmation screen briefly
        time.sleep(1.0)

        # Clean up browser pages without blocking on persistent context shutdown
        try:
            for p_item in list(context.pages):
                try:
                    p_item.close()
                except Exception:
                    pass
        except Exception:
            pass

        try:
            import threading
            t = threading.Thread(target=context.close, daemon=True)
            t.start()
            t.join(timeout=1.5)
        except Exception:
            pass

    if not captured_code and not exchange_result[0]:
        return False, f"Timed out after {timeout_sec}s waiting for Schwab login."

    if not exchange_result[0] and captured_code:
        ok, msg = sc.exchange_code_from_redirect(captured_code[0])
        return ok, msg

    return bool(exchange_result[0]), str(exchange_result[1])


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
