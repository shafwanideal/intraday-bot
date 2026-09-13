import os

from dotenv import load_dotenv

load_dotenv()

KITE_API_KEY = os.environ.get("KITE_API_KEY")
KITE_API_SECRET = os.environ.get("KITE_API_SECRET")

# Safety gate: real orders (src/live.py) refuse to run at all unless this is
# explicitly "true" in .env. Not set by .env.example on purpose -- must be a
# deliberate, separate opt-in, not something copy-pasted in by default.
LIVE_TRADING_ENABLED = os.environ.get("LIVE_TRADING_ENABLED", "").strip().lower() == "true"

SESSION_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".kite_session.json")

# Optional -- a ready-made access token, for environments that CAN'T run the
# interactive login: no browser, no one at the keyboard (a cloud/CI session, a
# headless box). Generate it wherever you can log in (python3 -m src.auth prints
# it and caches it in SESSION_FILE) and hand it over as an env var. It skips the
# OAuth flow entirely, so KITE_API_SECRET is NOT needed alongside it -- the
# secret only ever signs generate_session, and it should stay on the machine you
# log in from. Kite invalidates the token overnight, so this is a fresh value
# every trading day, not a permanent setting.
KITE_ACCESS_TOKEN = (os.environ.get("KITE_ACCESS_TOKEN") or "").strip() or None

# Optional -- Telegram alerts. src/telegram_notify.py no-ops entirely if either is unset.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def require_api_key() -> None:
    """The api_key alone is enough to USE an access token -- reading data or
    placing orders only needs the key plus a valid token. Only minting a new
    token (the OAuth exchange in src/auth.login) needs the secret too, which is
    what require_credentials() below is for."""
    if not KITE_API_KEY:
        raise RuntimeError(
            "Missing required environment variable KITE_API_KEY. Copy .env.example to .env "
            "and fill in your Kite Connect API key (find it at developers.kite.trade)."
        )


def require_credentials() -> None:
    missing = [
        name
        for name, value in (("KITE_API_KEY", KITE_API_KEY), ("KITE_API_SECRET", KITE_API_SECRET))
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "Copy .env.example to .env and fill in your Kite Connect credentials."
        )
