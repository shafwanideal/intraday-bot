import os

from dotenv import load_dotenv

load_dotenv()

KITE_API_KEY = os.environ.get("KITE_API_KEY")
KITE_API_SECRET = os.environ.get("KITE_API_SECRET")

SESSION_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".kite_session.json")


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
