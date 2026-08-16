import http.server
import json
import os
import threading
import webbrowser
from datetime import date
from urllib.parse import parse_qs, urlparse

from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException

from . import config

CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 8000  # must match the Redirect URL registered on developers.kite.trade
CALLBACK_TIMEOUT_SECONDS = 180


def _load_cached_session() -> str | None:
    if not os.path.exists(config.SESSION_FILE):
        return None
    with open(config.SESSION_FILE) as f:
        data = json.load(f)
    if data.get("date") != date.today().isoformat():
        return None
    return data.get("access_token")


def _save_session(access_token: str) -> None:
    with open(config.SESSION_FILE, "w") as f:
        json.dump({"access_token": access_token, "date": date.today().isoformat()}, f)
    os.chmod(config.SESSION_FILE, 0o600)


def _extract_request_token(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("http"):
        query = parse_qs(urlparse(raw).query)
        token = query.get("request_token", [None])[0]
        if not token:
            raise ValueError("No request_token found in the pasted URL.")
        return token
    return raw


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Catches Kite's OAuth redirect on 127.0.0.1 so the user doesn't have to
    copy-paste the URL back into the terminal."""

    request_token: str | None = None

    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        _CallbackHandler.request_token = query.get("request_token", [None])[0]
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        message = "Logged in — you can close this tab." if _CallbackHandler.request_token else "Login failed or was cancelled."
        self.wfile.write(f"<html><body><h2>{message}</h2></body></html>".encode())

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - matches base class signature
        pass  # suppress default per-request console logging


def _login_via_local_callback(login_url: str) -> str | None:
    """Open the browser and wait for Kite's redirect to land on our local
    server. Returns the request_token, or None if it timed out (caller
    should fall back to manual paste)."""
    _CallbackHandler.request_token = None
    try:
        server = http.server.HTTPServer((CALLBACK_HOST, CALLBACK_PORT), _CallbackHandler)
    except OSError:
        print(f"Couldn't bind {CALLBACK_HOST}:{CALLBACK_PORT} (already in use?) -- falling back to manual paste.")
        return None

    server_thread = threading.Thread(target=server.handle_request)
    server_thread.start()

    print(f"Opening your browser for Kite login: {login_url}")
    webbrowser.open(login_url)

    server_thread.join(timeout=CALLBACK_TIMEOUT_SECONDS)
    server.server_close()
    return _CallbackHandler.request_token


def login() -> str:
    """Run the interactive Kite Connect login flow and return a fresh access token.

    Kite access tokens are tied to the calendar day, so this must be re-run
    each trading day. Tries to catch the OAuth redirect automatically via a
    local server; falls back to manual copy-paste if that doesn't work.
    """
    config.require_credentials()
    kite = KiteConnect(api_key=config.KITE_API_KEY)
    login_url = kite.login_url()

    request_token = _login_via_local_callback(login_url)

    if not request_token:
        print("1. Open this URL in your browser and log in to Kite:")
        print(f"   {login_url}")
        print("2. After login, Kite redirects to your app's redirect URL with a request_token param.")
        raw = input("3. Paste the full redirect URL (or just the request_token) here: ")
        request_token = _extract_request_token(raw)

    try:
        session = kite.generate_session(request_token, api_secret=config.KITE_API_SECRET)
    except KiteException as exc:
        raise RuntimeError(f"Kite login failed: {exc}") from exc

    access_token = session["access_token"]
    _save_session(access_token)
    print(f"Access token saved to {config.SESSION_FILE} for today.")
    return access_token


def get_kite() -> KiteConnect:
    """Return an authenticated KiteConnect client, reusing today's cached token if present."""
    config.require_credentials()
    kite = KiteConnect(api_key=config.KITE_API_KEY)

    access_token = _load_cached_session()
    if not access_token:
        access_token = login()

    kite.set_access_token(access_token)
    return kite


if __name__ == "__main__":
    client = get_kite()
    profile = client.profile()
    print(f"Authenticated as {profile['user_name']} ({profile['user_id']})")
