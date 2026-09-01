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

# Optional -- Telegram alerts. src/telegram_notify.py no-ops entirely if either is unset.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


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
