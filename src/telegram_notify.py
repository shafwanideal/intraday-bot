"""Telegram alerts for live trading -- hourly updates, unusual-movement
alerts, and large/mid-cap "consider averaging" suggestions. Entirely
optional: if TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID aren't set, every function
here silently no-ops rather than raising, so live.py/shadow.py work exactly
as before for anyone who hasn't set this up.

Never let a Telegram failure affect real trading -- every call here is
wrapped so a network hiccup or bad token just gets logged and skipped, not
propagated up into the polling loop.
"""

import requests

from . import config

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
REQUEST_TIMEOUT_SECONDS = 10


def enabled() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def send_message(text: str) -> bool:
    """Best-effort send -- returns True on success, False (and prints a
    warning) on any failure. Never raises."""
    if not enabled():
        return False
    try:
        resp = requests.post(
            TELEGRAM_API_URL.format(token=config.TELEGRAM_BOT_TOKEN),
            data={"chat_id": config.TELEGRAM_CHAT_ID, "text": text},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            print(f"WARNING: Telegram send failed ({resp.status_code}): {resp.text}")
            return False
        return True
    except requests.exceptions.RequestException as exc:
        print(f"WARNING: Telegram send failed: {exc}")
        return False
