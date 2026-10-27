import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from dotenv import dotenv_values
from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException

from . import config

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Must match the Redirect URL registered on developers.kite.trade EXACTLY (host, port, path).
# kite_login.py listens on that port/path, so the Kite console never needs changing.
REDIRECT_URL = os.environ.get("KITE_REDIRECT_URL", "http://127.0.0.1:8000/")


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


def _token_is_live(token: str) -> bool:
    """Ask Kite whether a token still works. kite_login.py keeps today's token in
    .env, so by next morning the saved one is stale -- presence alone proves nothing."""
    try:
        kite = KiteConnect(api_key=config.KITE_API_KEY)
        kite.set_access_token(token)
        kite.profile()
        return True
    except (KiteException, requests.exceptions.RequestException):
        return False


def has_valid_session() -> bool:
    """True if get_kite() would succeed without an interactive login, so
    callers can tell 'needs login' from 'ready' without triggering one.

    An env/.env token only counts if Kite still accepts it (it is stale every
    morning); otherwise fall back to today's cached session file."""
    if config.KITE_ACCESS_TOKEN and _token_is_live(config.KITE_ACCESS_TOKEN):
        return True
    return _load_cached_session() is not None


def _adopt_token(access_token: str) -> None:
    """Make a freshly minted token the one this process (and any subprocess it
    spawns) uses. config.py read the OLD .env value at import and load_dotenv()
    never overrides an existing os.environ entry, so without this a child like
    run_live.py would inherit yesterday's token (same trap as LIVE_TRADING_ENABLED
    in scripts/daily.py)."""
    os.environ["KITE_ACCESS_TOKEN"] = access_token
    config.KITE_ACCESS_TOKEN = access_token
    _save_session(access_token)


def login() -> str:
    """Daily Kite login via kite_login.py, which listens for Kite's redirect on
    REDIRECT_URL and writes the new token into .env.

    Meant for the VPS: run it with an SSH tunnel from your own machine
    (`ssh -L 8000:127.0.0.1:8000 user@vps`) so the browser redirect on your
    machine reaches the listener here. --no-browser because a headless server
    has no browser to open (and webbrowser.open can launch a text browser there).
    The password/2FA are typed by you on Zerodha's own page.
    """
    config.require_credentials()
    script = PROJECT_ROOT / "kite_login.py"
    result = subprocess.run(
        [sys.executable, str(script), "login", "--redirect", REDIRECT_URL, "--no-browser"],
        cwd=PROJECT_ROOT,
    )
    token = (dotenv_values(PROJECT_ROOT / ".env").get("KITE_ACCESS_TOKEN") or "").strip()
    if result.returncode != 0 or not token:
        raise RuntimeError("Kite login did not complete -- see the output above. Re-run: python3 kite_login.py --redirect " + REDIRECT_URL)
    _adopt_token(token)
    return token


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
            f"run `python3 kite_login.py --redirect {REDIRECT_URL}` to get today's."
        ) from exc
    print(f"Using KITE_ACCESS_TOKEN from the environment (authenticated as {profile['user_name']}).")
    return kite


def get_kite(interactive: bool = True) -> KiteConnect:
    """Return an authenticated KiteConnect client.

    Token precedence: a KITE_ACCESS_TOKEN (env var or .env) that Kite still accepts, then today's
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
        try:
            return _client_from_env_token(config.KITE_ACCESS_TOKEN)
        except RuntimeError as exc:
            if not interactive:
                raise
            print(f"{exc}\nSaved token is stale -- logging in again.")

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

    if not has_valid_session():
        login()
    client = get_kite()
    profile = client.profile()
    print(f"Authenticated as {profile['user_name']} ({profile['user_id']})")
