"""
Schwab Auto-Auth 1-Click Receiver (schwab_auto_auth.py)
======================================================
Automates the Schwab 7-day OAuth token renewal with zero manual copy-pasting:
1. Ensures local self-signed SSL certs exist for 127.0.0.1.
2. Spawns a temporary HTTPS listener on 127.0.0.1:443.
3. Automatically launches the official Schwab login page in your default browser.
4. When Schwab redirects to https://127.0.0.1/?code=..., the listener captures the code
   within milliseconds, exchanges it for a 7-day token, and saves schwab_tokens.json.
5. Shuts down the temporary listener cleanly and shows a success confirmation in the browser.
"""

from __future__ import annotations

import datetime
import http.server
import ipaddress
import os
import ssl
import sys
import threading
import time
import urllib.parse
import webbrowser
from typing import Optional, Tuple

from live_market_data import SchwabReadOnlyClient, TokenBucketRateLimiter, _load_local_dotenv, get_app_credential


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CERT_FILE = os.path.join(BASE_DIR, "schwab_cert.pem")
KEY_FILE = os.path.join(BASE_DIR, "schwab_key.pem")


def ensure_ssl_certificates() -> Tuple[str, str]:
    """Generates a local 127.0.0.1 self-signed certificate if not present."""
    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        return CERT_FILE, KEY_FILE

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    with open(CERT_FILE, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(KEY_FILE, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
    return CERT_FILE, KEY_FILE


SUCCESS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Schwab Connected Successfully</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: #0b1329;
      color: #e2e8f0;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      padding: 20px;
    }
    .card {
      background: #0f172a;
      border: 2px solid #38bdf8;
      border-radius: 20px;
      padding: 40px;
      max-width: 540px;
      width: 100%;
      text-align: center;
      box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.7);
    }
    .icon {
      font-size: 56px;
      line-height: 1;
      margin-bottom: 16px;
    }
    h1 {
      color: #4ade80;
      font-size: 26px;
      font-weight: 900;
      margin-bottom: 12px;
    }
    p {
      color: #94a3b8;
      font-size: 14px;
      line-height: 1.6;
      margin-bottom: 24px;
    }
    .badge {
      display: inline-block;
      background: #042f2e;
      color: #2dd4bf;
      border: 1px solid #14b8a6;
      font-weight: 700;
      font-size: 12px;
      padding: 6px 14px;
      border-radius: 9999px;
      margin-bottom: 24px;
    }
    .btn {
      display: inline-block;
      background: linear-gradient(135deg, #38bdf8 0%, #0284c7 100%);
      color: #000;
      font-weight: 900;
      font-size: 14px;
      padding: 12px 28px;
      border-radius: 12px;
      text-decoration: none;
      transition: all 0.2s ease;
    }
    .btn:hover {
      transform: translateY(-2px);
      box-shadow: 0 10px 20px rgba(56, 189, 248, 0.4);
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">&#9989;</div>
    <h1>Schwab Market Data &amp; LEAPs Connected!</h1>
    <div class="badge">7-Day Token Active &bull; Real Exchange Feeds Live</div>
    <p>
      Your authorization was captured and exchanged automatically. Live option chains, accurate LEAP dates, and real broker Greeks are now fully active.
    </p>
    <a href="http://localhost:8765" class="btn">&#8594; Return to Dashboard</a>
  </div>
</body>
</html>
"""

ERROR_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Schwab Connection Error</title>
  <style>
    body {{ background: #0b1329; color: #f87171; font-family: sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; text-align: center; }}
    .card {{ background: #1e293b; border: 2px solid #ef4444; border-radius: 16px; padding: 36px; max-width: 500px; }}
    h1 {{ color: #f87171; margin-bottom: 12px; }}
    p {{ color: #cbd5e1; font-size: 14px; line-height: 1.5; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Authorization Failed</h1>
    <p>{error_msg}</p>
  </div>
</body>
</html>
"""


class SecureHTTPServer(http.server.HTTPServer):
    """Properly wraps accepted sockets with TLS on Python 3.12+."""
    def __init__(self, server_address, RequestHandlerClass, ssl_context):
        super().__init__(server_address, RequestHandlerClass)
        self.ssl_context = ssl_context

    def get_request(self):
        newsocket, fromaddr = self.socket.accept()
        conn = self.ssl_context.wrap_socket(newsocket, server_side=True)
        return conn, fromaddr


class SchwabAutoAuthManager:
    """Manages 1-click background HTTPS listener on 127.0.0.1:443."""

    def __init__(self):
        self.cert_path, self.key_path = ensure_ssl_certificates()
        self.server: Optional[SecureHTTPServer] = None
        self.captured_result: Optional[Tuple[bool, str]] = None
        self._done_event = threading.Event()
        self._is_running = False

    def start_listener(self, port: int = 443, timeout_seconds: int = 180) -> bool:
        """Starts the HTTPS listener on 127.0.0.1:443 in a background thread."""
        if self._is_running:
            return True

        self.captured_result = None
        self._done_event.clear()

        manager = self

        class CaptureHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                qs = urllib.parse.parse_qs(parsed.query)
                code = qs.get("code", [""])[0]

                if not code:
                    self.send_response(400)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(ERROR_HTML_TEMPLATE.format(error_msg="No authorization code received in callback URL.").encode("utf-8"))
                    return

                # Perform exchange immediately
                from live_market_data import SchwabReadOnlyClient, TokenBucketRateLimiter
                sc = SchwabReadOnlyClient(TokenBucketRateLimiter(8, 60))
                ok, msg = sc.exchange_code_from_redirect(code)
                manager.captured_result = (ok, msg)

                if ok:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(SUCCESS_HTML.encode("utf-8"))
                else:
                    self.send_response(400)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(ERROR_HTML_TEMPLATE.format(error_msg=msg).encode("utf-8"))

                manager._done_event.set()

            def log_message(self, format, *args):
                pass

        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(self.cert_path, self.key_path)

            self.server = SecureHTTPServer(("127.0.0.1", port), CaptureHandler, ctx)
            self._is_running = True

            def _serve_loop():
                while not manager._done_event.is_set() and manager._is_running:
                    try:
                        manager.server.timeout = 1.0
                        manager.server.handle_request()
                    except Exception:
                        continue
                manager._is_running = False
                try:
                    manager.server.server_close()
                except Exception:
                    pass

            t = threading.Thread(target=_serve_loop, daemon=True)
            t.start()
            return True
        except Exception as e:
            sys.stderr.write(f"[AUTO-AUTH ERROR] Could not bind HTTPS on 127.0.0.1:{port}: {e}\n")
            self._is_running = False
            return False

    def wait_for_completion(self, timeout_seconds: int = 120) -> Tuple[bool, str]:
        """Waits for user to complete the browser login."""
        completed = self._done_event.wait(timeout=timeout_seconds)
        self._is_running = False
        if completed and self.captured_result:
            return self.captured_result
        return False, f"Timed out after {timeout_seconds} seconds waiting for Schwab login."

    def stop(self):
        self._is_running = False
        self._done_event.set()
        if self.server:
            try:
                self.server.server_close()
            except Exception:
                pass


def run_interactive_1click_auth(timeout: int = 150) -> Tuple[bool, str]:
    """1-Click Reauth: Launches browser, starts listener, catches code, returns result."""
    manager = SchwabAutoAuthManager()
    started = manager.start_listener(port=443, timeout_seconds=timeout)
    if not started:
        return False, "Could not start local HTTPS listener on 127.0.0.1:443. Ensure no other service is using port 443."

    from live_market_data import SchwabReadOnlyClient, TokenBucketRateLimiter
    sc = SchwabReadOnlyClient(TokenBucketRateLimiter(8, 60))
    auth_url = sc.get_auth_url()
    if not auth_url:
        manager.stop()
        return False, "SCHWAB_APP_KEY and SCHWAB_APP_SECRET not found in environment."

    print("\n=======================================================")
    print("   SCHWAB 1-CLICK MARKET DATA & LEAPS AUTHENTICATION   ")
    print("=======================================================")
    print("[1] Opening official Schwab login page in your default browser...")
    print(f"[2] Local receiver listening on https://127.0.0.1:443 (Waiting up to {timeout}s)...")
    print("\n* NOTE: After you click 'Approve', if your browser displays")
    print("  'Your connection is not private' (due to local self-signed cert),")
    print("  simply click: Advanced -> 'Proceed to 127.0.0.1 (unsafe)'.")
    print("  The receiver will immediately capture your token in 0.1s!")
    print("=======================================================\n")

    try:
        webbrowser.open(auth_url)
    except Exception:
        print(f"Could not open browser automatically. Please open this URL manually:\n{auth_url}\n")

    ok, msg = manager.wait_for_completion(timeout_seconds=timeout)
    return ok, msg


if __name__ == "__main__":
    import truststore
    truststore.inject_into_ssl()
    ok, msg = run_interactive_1click_auth()
    if ok:
        print(f"\n\033[92;1m[SUCCESS]: {msg}\033[0m\n")
        sys.exit(0)
    else:
        print(f"\n\033[91;1m[FAILED]: {msg}\033[0m\n")
        sys.exit(1)
