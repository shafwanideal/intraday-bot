"""Phone push alerts via ntfy.sh -- no bot token, just an unguessable topic.

Setup: install the free "ntfy" app, subscribe to your topic name, and put
NTFY_TOPIC=<that name> in .env. Anyone who knows the topic can read AND post
to it, so make it long and random (e.g. `openssl rand -hex 8`) and treat it
like a password. Messages carry symbols/prices, never keys.
"""
import os

import requests
from dotenv import load_dotenv

load_dotenv()

SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")


def topic() -> str | None:
    return os.getenv("NTFY_TOPIC") or None


def enabled() -> bool:
    return topic() is not None


def send(text: str, title: str = "Groww guardian", priority: str = "default", tags: str = "") -> None:
    """Best-effort push. Never raises -- an alert outage must not stop tracking."""
    t = topic()
    if t is None:
        return
    headers = {"Title": title.encode("ascii", "ignore").decode(), "Priority": priority}
    if tags:
        headers["Tags"] = tags
    try:
        requests.post(f"{SERVER}/{t}", data=text.encode("utf-8"), headers=headers, timeout=10)
    except requests.RequestException as exc:
        print(f"WARNING: ntfy push failed: {exc}")
