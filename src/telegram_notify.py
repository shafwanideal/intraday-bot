"""Telegram transport -- outbound alerts and inbound command polling.

Outbound: hourly updates, unusual-movement alerts, and averaging
suggestions. Entirely optional: if TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID
aren't set, every function here silently no-ops rather than raising, so
live.py/shadow.py work exactly as before for anyone who hasn't set this up.

Inbound: get_updates() long-polls for messages so src/telegram_control.py
can be driven from a phone. Every inbound message is checked against
TELEGRAM_CHAT_ID by is_authorized() before it is acted on -- this bot can
place real orders, so a message from any other chat must be dropped, not
merely ignored by convention.

Never let a Telegram failure affect real trading -- every call here is
wrapped so a network hiccup or bad token just gets logged and skipped, not
propagated up into the polling loop.
"""

import requests

from . import config

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_UPDATES_URL = "https://api.telegram.org/bot{token}/getUpdates"
REQUEST_TIMEOUT_SECONDS = 10
# Long-poll: Telegram holds the request open until a message arrives or this
# elapses, so the control bot reacts within a second of a message without
# hammering the API on an idle afternoon.
LONG_POLL_TIMEOUT_SECONDS = 30


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


def is_authorized(chat_id) -> bool:
    """Only the configured chat may command this bot. The control bot can
    place real orders, so an unrecognised chat_id is dropped outright --
    there is no "read-only" tier that would be safe to expose to a stranger
    who guessed the bot's handle."""
    return bool(config.TELEGRAM_CHAT_ID) and str(chat_id) == str(config.TELEGRAM_CHAT_ID)


def get_updates(offset: int | None = None, timeout: int = LONG_POLL_TIMEOUT_SECONDS) -> list[dict]:
    """Long-poll for inbound messages. Returns a (possibly empty) list of
    Telegram update dicts. Never raises -- a network blip just yields no
    updates this round and the caller polls again."""
    if not enabled():
        return []
    params: dict[str, object] = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    try:
        resp = requests.get(
            TELEGRAM_UPDATES_URL.format(token=config.TELEGRAM_BOT_TOKEN),
            params=params,
            # Outlast the server-side long poll, or every single call would
            # time out client-side before Telegram ever got a chance to reply.
            timeout=timeout + REQUEST_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            print(f"WARNING: Telegram getUpdates failed ({resp.status_code}): {resp.text}")
            return []
        return resp.json().get("result", [])
    except (requests.exceptions.RequestException, ValueError) as exc:
        print(f"WARNING: Telegram getUpdates failed: {exc}")
        return []
