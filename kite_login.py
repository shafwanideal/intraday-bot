#!/usr/bin/env python3
"""
Daily Zerodha Kite Connect login -- one command, no copy-paste.

    python3 kite_login.py           # open the login page, catch the redirect, save the token
    python3 kite_login.py check     # verify the saved token still works
    python3 kite_login.py serve     # ALWAYS-ON listener (run under PM2/systemd on a server):
                                    # after that, logging in is just clicking a bookmark

How it works: Kite redirects to the Redirect URL registered on your Kite Connect
app, carrying a one-time request_token. This script runs a tiny web server on
127.0.0.1 (your own machine only) to receive that redirect, swaps the token for
an access_token with your API secret, and saves it to .env next to this file.

The Redirect URL in the Kite developer console must be exactly:
    http://127.0.0.1:5099/zerodha_callback
or, if the Kite app already has a localhost Redirect URL you want to keep, pass it:
    python3 kite_login.py --redirect http://127.0.0.1:8000/<its path>

The password + 2FA are typed by you on Zerodha's own page (SEBI requires this;
it is never automated). The access token expires around 6 AM every day, so run
this once each morning before the algo/scripts need to talk to Kite.

.env (same folder, keep it private, never share or commit it):
    KITE_API_KEY=...
    KITE_API_SECRET=...
    KITE_ACCESS_TOKEN=          <- filled in by this script
    KITE_USER_ID=               <- optional: only accept logins for this Zerodha ID (e.g. AB1234)

Needs:  pip install kiteconnect
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from kiteconnect import KiteConnect

ENV_PATH = Path(__file__).resolve().parent / ".env"
HOST, DEFAULT_PORT, DEFAULT_PATH = "127.0.0.1", 5099, "/zerodha_callback"
WAIT_SECONDS = 300
# Optional: if this script exists, it is run (detached) right after every successful login,
# e.g. to start the guardians. A failure here never affects the login itself.
POST_LOGIN_HOOK = Path(__file__).resolve().parent / "scripts" / "on_login.sh"


def read_env() -> dict:
    out = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def update_env(key: str, value: str) -> None:
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    out, found = [], False
    for line in lines:
        if line.startswith(f"{key}="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    tmp = ENV_PATH.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(ENV_PATH)


def run_post_login_hook() -> None:
    if not POST_LOGIN_HOOK.is_file():
        return
    try:
        log = open(ENV_PATH.parent / "logs" / "on_login.log", "ab") if (ENV_PATH.parent / "logs").is_dir() else subprocess.DEVNULL
        subprocess.Popen(["bash", str(POST_LOGIN_HOOK)], cwd=str(ENV_PATH.parent), stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, start_new_session=True)
        print(f"Ran post-login hook {POST_LOGIN_HOOK.name}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"Post-login hook failed to start: {e}", flush=True)


def credentials() -> tuple[str, str]:
    env = read_env()
    key, secret = env.get("KITE_API_KEY"), env.get("KITE_API_SECRET")
    if not key or not secret:
        sys.exit(f"KITE_API_KEY / KITE_API_SECRET missing in {ENV_PATH}\n"
                 "Open that file and fill them in from developers.kite.trade.")
    return key, secret


def make_handler(kite: KiteConnect, api_secret: str, callback_path: str, result: dict, done: threading.Event | None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep the terminal quiet
            pass

        def _page(self, code: int, html: str) -> None:
            body = html.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == callback_path.rstrip("/") + "_health":
                return self._page(200, "ok")
            if u.path != callback_path:
                return self._page(404, "Not found")
            q = parse_qs(u.query)
            token, status = q.get("request_token", [None])[0], q.get("status", [None])[0]
            if status != "success" or not token:
                result["error"] = f"Kite reported status={status}"
                self._page(400, "<h3>Login did not succeed.</h3><p>Close this tab and click the login bookmark again.</p>")
            else:
                try:
                    data = kite.generate_session(token, api_secret=api_secret)
                    # The api_key is public (it is in the login URL), so on an internet-facing
                    # listener anyone could log in to this app with THEIR Zerodha account and
                    # overwrite the saved token. If KITE_USER_ID is set in .env, only that
                    # account's login is accepted.
                    expected = read_env().get("KITE_USER_ID")
                    if expected and data.get("user_id") != expected:
                        result["error"] = "login was for a different Zerodha account"
                        print(f"Rejected a login for {data.get('user_id')} (expected {expected}); token NOT saved.", flush=True)
                        self._page(403, "<h3>Wrong Zerodha account.</h3><p>Token not saved.</p>")
                        if done:
                            done.set()
                        return
                    update_env("KITE_ACCESS_TOKEN", data["access_token"])
                    run_post_login_hook()
                    result["ok"] = f"{data.get('user_name')} ({data.get('user_id')})"
                    print(f"AUTH OK. Logged in as {result['ok']}. KITE_ACCESS_TOKEN saved to {ENV_PATH}", flush=True)
                    self._page(200, f"<h3>Zerodha login OK.</h3><p>{result['ok']}. "
                                    "Token saved - you can close this tab.</p>")
                except Exception as e:
                    result["error"] = str(e)
                    print(f"Token exchange failed: {e}", flush=True)
                    self._page(500, f"<h3>Token exchange failed.</h3><p>{e}</p>")
            if done:
                done.set()
    return Handler


def login(port: int, callback_path: str, open_browser: bool = True) -> None:
    api_key, api_secret = credentials()
    kite = KiteConnect(api_key=api_key)
    result: dict = {}
    done = threading.Event()
    try:
        server = HTTPServer((HOST, port), make_handler(kite, api_secret, callback_path, result, done))
    except OSError as e:
        sys.exit(f"Could not listen on {HOST}:{port} ({e}). Is another copy running? "
                 "Use --port only if you also change the Redirect URL on the Kite app.")
    threading.Thread(target=server.serve_forever, daemon=True).start()

    url = kite.login_url()
    if open_browser:
        print("Opening the Zerodha login page. If it does not open, paste this into your browser:\n")
    else:
        print("Open this URL in your browser and log in:\n")
    print(f"  {url}\n")
    print(f"Waiting up to {WAIT_SECONDS // 60} minutes for you to log in (password + 2FA)...")
    if open_browser:
        webbrowser.open(url)

    done.wait(WAIT_SECONDS)
    server.shutdown()
    if "ok" in result:
        print(f"AUTH OK. Logged in as {result['ok']}. KITE_ACCESS_TOKEN saved to {ENV_PATH}")
    else:
        sys.exit(f"Login failed: {result.get('error', 'timed out waiting for the redirect')}\n"
                 f"Check the Redirect URL on your Kite app is exactly http://{HOST}:{port}{callback_path}")


def serve(port: int, callback_path: str) -> None:
    """Always-on listener: every successful Zerodha login (from any bookmark/browser) refreshes the token."""
    api_key, api_secret = credentials()
    kite = KiteConnect(api_key=api_key)
    server = HTTPServer((HOST, port), make_handler(kite, api_secret, callback_path, {}, None))
    print(f"Listening on {HOST}:{port}{callback_path} (daily login bookmark: {kite.login_url()})", flush=True)
    server.serve_forever()


def check() -> None:
    api_key, _ = credentials()
    token = read_env().get("KITE_ACCESS_TOKEN")
    if not token:
        sys.exit("No KITE_ACCESS_TOKEN saved yet - run: python3 kite_login.py")
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(token)
    try:
        p = kite.profile()
    except Exception as e:
        sys.exit(f"Token not valid (expired after ~6 AM, or revoked): {e}\nRun: python3 kite_login.py")
    print(f"Token OK - logged in as {p.get('user_name')} ({p.get('user_id')})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", nargs="?", choices=["login", "check", "serve"], default="login")
    ap.add_argument("--redirect", help="the Redirect URL ALREADY set on your Kite app, e.g. "
                    "http://127.0.0.1:8000/callback -- the script listens on that port and path, "
                    "so nothing needs changing in the Kite console")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true",
                    help="just print the login URL (use on a headless server)")
    a = ap.parse_args()
    port, path = a.port, DEFAULT_PATH
    if a.redirect:
        r = urlparse(a.redirect)
        if r.hostname not in ("127.0.0.1", "localhost"):
            sys.exit("--redirect must point at this computer (127.0.0.1 or localhost).")
        port, path = r.port or 80, r.path or "/"
    if a.action == "check":
        check()
    elif a.action == "serve":
        serve(port, path)
    else:
        login(port, path, open_browser=not a.no_browser)
