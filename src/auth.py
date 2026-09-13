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

    print(f"Open this URL to log in to Kite: {login_url}")
    try:
        if not webbrowser.open(login_url):
            raise webbrowser.Error("no browser available")
    except webbrowser.Error:
        print(
            "(Couldn't auto-open a browser here -- normal on a headless server. "
            "Open the URL above in your own browser; if you're SSH'd in with "
            "`-L 8000:localhost:8000` port forwarding, the redirect will reach "
            "this script automatically.)"
        )

    server_thread.join(timeout=CALLBACK_TIMEOUT_SECONDS)
    server.server_close()
    return _CallbackHandler.request_token


def build_login_url() -> str:
    """The Kite OAuth URL the user must open in a browser. Split out of
    login() so the control bot can send it to a phone instead of printing it
    to a terminal nobody is watching."""
    config.require_credentials()
    return KiteConnect(api_key=config.KITE_API_KEY).login_url()


def complete_login(raw: str) -> str:
    """Exchange a pasted redirect URL (or a bare request_token) for today's
    access token and cache it. Returns the access token.

    This is the half of login() that has no terminal in it, so it works
    identically whether the request_token arrived via the local callback
    server, a terminal paste, or a Telegram message."""
    config.require_credentials()
    kite = KiteConnect(api_key=config.KITE_API_KEY)
    request_token = _extract_request_token(raw)
    try:
        session = kite.generate_session(request_token, api_secret=config.KITE_API_SECRET)
    except KiteException as exc:
        raise RuntimeError(f"Kite login failed: {exc}") from exc
    access_token = session["access_token"]
    _save_session(access_token)
    return access_token


def has_valid_session() -> bool:
    """True if get_kite() would succeed without an interactive login, so
    callers can tell 'needs login' from 'ready' without triggering one.

    Mirrors get_kite()'s precedence: an env token counts as a session even
    though it never touches SESSION_FILE. Checking only the cache would make
    the control bot demand /login on a box that was handed KITE_ACCESS_TOKEN
    and is perfectly able to trade.
    """
    return bool(config.KITE_ACCESS_TOKEN) or _load_cached_session() is not None


def login(paste_only: bool = False) -> str:
    """Run the interactive Kite Connect login flow and return a fresh access token.

    Kite access tokens are tied to the calendar day, so this must be re-run
    each trading day. Tries to catch the OAuth redirect automatically via a
    local server; falls back to manual copy-paste if that doesn't work.

    On a headless box (Contabo) the callback server is only reachable if
    you SSH'd in with `-L 8000:localhost:8000`. Without that tunnel the wait
    is a guaranteed CALLBACK_TIMEOUT_SECONDS of dead air before the paste
    prompt appears, so paste_only=True skips straight to the prompt.
    """
    login_url = build_login_url()

    request_token = None if paste_only else _login_via_local_callback(login_url)

    if not request_token:
        print("1. Open this URL in your browser and log in to Kite:")
        print(f"   {login_url}")
        print("2. After login, Kite redirects to your app's redirect URL with a request_token param.")
        request_token = input("3. Paste the full redirect URL (or just the request_token) here: ")

    access_token = complete_login(request_token)
    print(f"Access token saved to {config.SESSION_FILE} for today.")
    # Printed so it can be carried to an environment that can't run this flow
    # (see config.KITE_ACCESS_TOKEN). It's a live credential for the rest of
    # today -- it can place orders, not just read data -- so don't paste it
    # anywhere it will be logged or persisted beyond tonight.
    print(f"  KITE_ACCESS_TOKEN={access_token}")
    return access_token


def _client_from_env_token(access_token: str) -> KiteConnect:
    """Use a token minted elsewhere (KITE_ACCESS_TOKEN) instead of logging in.

    Deliberately NOT written to SESSION_FILE: the env var is a per-session
    choice, and caching it would silently outlive the environment it was set
    in. The profile() call is a cheap up-front check -- an expired token is
    the normal case here (Kite kills them overnight), and failing now with a
    clear message beats failing halfway through a backtest with Kite's
    generic "Incorrect `api_key` or `access_token`".
    """
    config.require_api_key()  # the secret is only needed to MINT a token, not to use one
    kite = KiteConnect(api_key=config.KITE_API_KEY)
    kite.set_access_token(access_token)
    try:
        profile = kite.profile()
    except KiteException as exc:
        raise RuntimeError(
            f"KITE_ACCESS_TOKEN was rejected by Kite ({exc}). Tokens expire overnight -- "
            "run `python3 -m src.auth` where you can log in, and set KITE_ACCESS_TOKEN to "
            "the fresh token (it's printed there and stored in .kite_session.json)."
        ) from exc
    print(f"Using KITE_ACCESS_TOKEN from the environment (authenticated as {profile['user_name']}).")
    return kite


def get_kite(interactive: bool = True) -> KiteConnect:
    """Return an authenticated KiteConnect client.

    Token precedence: an explicit KITE_ACCESS_TOKEN env var, then today's
    cached session file, then an interactive login. The env var wins because
    setting it is a deliberate per-session act -- and in the environments that
    need it (headless/cloud), the login it would otherwise fall through to
    can't run at all.

    interactive=False raises instead of prompting at that last step, for
    callers with no terminal to prompt on (the Telegram control bot, or
    anything under systemd), where blocking on input() would hang the process
    forever rather than fail visibly.
    """
    if config.KITE_ACCESS_TOKEN:
        return _client_from_env_token(config.KITE_ACCESS_TOKEN)

    config.require_credentials()
    kite = KiteConnect(api_key=config.KITE_API_KEY)

    access_token = _load_cached_session()
    if not access_token:
        if not interactive:
            raise RuntimeError(
                "No valid Kite session for today and no terminal to log in from. "
                "Send /login to the Telegram bot, or run `python3 -m src.auth` on the server."
            )
        access_token = login()

    kite.set_access_token(access_token)
    return kite


if __name__ == "__main__":
    import sys

    if "--paste" in sys.argv and not has_valid_session():
        login(paste_only=True)
    client = get_kite()
    profile = client.profile()
    print(f"Authenticated as {profile['user_name']} ({profile['user_id']})")
