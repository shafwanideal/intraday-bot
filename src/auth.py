import json
import os
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from dotenv import dotenv_values
from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException

from . import config

PROJECT_ROOT = Path(__file__).resolve().parent.parent


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


def _saved_token() -> str | None:
    """Today's token as kite_login.py (the always-on listener) last wrote it.
    Read from .env FRESH each time rather than trusting config.KITE_ACCESS_TOKEN,
    which is frozen at import: a long-lived process, or a child of daily.py that
    inherited yesterday's value through os.environ (load_dotenv never overrides),
    would otherwise keep using a stale token after you log in."""
    fresh = (dotenv_values(PROJECT_ROOT / ".env").get("KITE_ACCESS_TOKEN") or "").strip()
    return fresh or config.KITE_ACCESS_TOKEN


def _login_hint() -> str:
    return (
        f"Log in via your Kite login bookmark ({_bookmark_url()}), "
        "then run this again. This bot never starts a login itself."
    )


def _bookmark_url() -> str:
    return f"https://kite.zerodha.com/connect/login?api_key={config.KITE_API_KEY}&v=3"


def _token_is_live(token: str) -> bool:
    """Ask Kite whether a token still works. It is stale every morning, so its
    presence in .env proves nothing."""
    try:
        kite = KiteConnect(api_key=config.KITE_API_KEY)
        kite.set_access_token(token)
        kite.profile()
        return True
    except (KiteException, requests.exceptions.RequestException):
        return False


def refresh_client_if_changed(kite: KiteConnect) -> KiteConnect:
    """For the long-running guardians: if a newer token has been saved to .env since
    `kite` was built (you clicked the login bookmark again, or it is a new day),
    return a client on it; otherwise return `kite` itself. Kite replaces the old token
    on every login, so a guardian that kept its original client would go dark the
    moment a fresh login happened. Never raises -- a failed refresh keeps the old client."""
    try:
        token = _saved_token()
        if token and token != getattr(kite, "access_token", None) and _token_is_live(token):
            fresh = KiteConnect(api_key=config.KITE_API_KEY)
            fresh.set_access_token(token)
            return fresh
    except Exception:  # noqa: BLE001 - must never take a guardian down
        pass
    return kite


def has_valid_session() -> bool:
    """True if get_kite() would succeed, so callers can tell 'needs login' from
    'ready' without raising. A .env token only counts if Kite still accepts it;
    otherwise fall back to today's cached session file (the Telegram /login path)."""
    token = _saved_token()
    if token and _token_is_live(token):
        return True
    return _load_cached_session() is not None


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
            f"KITE_ACCESS_TOKEN in .env was rejected by Kite ({exc}) -- it is stale (tokens expire "
            f"overnight). {_login_hint()}"
        ) from exc
    print(f"Using KITE_ACCESS_TOKEN from the environment (authenticated as {profile['user_name']}).")
    return kite


def get_kite(interactive: bool = True) -> KiteConnect:
    """Return an authenticated KiteConnect client from the token saved in .env.

    Token precedence: the KITE_ACCESS_TOKEN in .env (written by the always-on
    kite_login.py listener when you click the login bookmark), then today's
    cached session file (the Telegram /login path). If neither works this RAISES
    with a clear message -- it never starts a login of its own, because a bot
    (or a scheduled script) blocking on a login nobody is attending is the
    silent failure to avoid. `interactive` is kept only so existing callers
    don't change; it no longer does anything.
    """
    token = _saved_token()
    if token:
        try:
            return _client_from_env_token(token)
        except RuntimeError:
            cached = _load_cached_session()
            if not cached:
                raise
            token = cached
    else:
        token = _load_cached_session()
        if not token:
            raise RuntimeError(f"No Kite token saved for today. {_login_hint()}")

    config.require_api_key()
    kite = KiteConnect(api_key=config.KITE_API_KEY)
    kite.set_access_token(token)
    return kite


if __name__ == "__main__":
    import sys

    client = get_kite()
    profile = client.profile()
    print(f"Authenticated as {profile['user_name']} ({profile['user_id']})")
