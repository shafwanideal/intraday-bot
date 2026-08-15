import json
import os
from datetime import date
from urllib.parse import parse_qs, urlparse

from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException

from . import config


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


def login() -> str:
    """Run the interactive Kite Connect login flow and return a fresh access token.

    Kite access tokens are tied to the calendar day, so this must be re-run
    each trading day.
    """
    config.require_credentials()
    kite = KiteConnect(api_key=config.KITE_API_KEY)

    print("1. Open this URL in your browser and log in to Kite:")
    print(f"   {kite.login_url()}")
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
