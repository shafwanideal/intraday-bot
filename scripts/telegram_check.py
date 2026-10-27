"""Diagnose the Telegram path end to end, without printing the token.

Every send has been failing 401 Unauthorized since at least 2026-10-27,
which means Telegram is rejecting the bot token outright -- the guardians
can place real exit orders and say nothing about it. This separates the
three things that look identical from the outside: a malformed token, a
revoked/deleted bot, and a wrong chat id.

    .venv/bin/python3 scripts/telegram_check.py
"""

import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402

TOKEN = config.TELEGRAM_BOT_TOKEN
CHAT = config.TELEGRAM_CHAT_ID
problems = []


def mask(tok: str) -> str:
    """A Telegram token is <bot_id>:<35-char secret>. Showing the bot id and
    the length is enough to spot truncation, stray quotes or whitespace
    without putting the secret on screen."""
    if not tok:
        return "(not set)"
    head, sep, tail = tok.partition(":")
    return f"{head}:{'*' * len(tail)}  (bot id {head}, secret {len(tail)} chars)" if sep else \
           f"{'*' * len(tok)}  (NO COLON -- not a valid token shape)"


print("=" * 72)
print("1. WHAT IS IN .env")
print("=" * 72)
print(f"  TELEGRAM_BOT_TOKEN : {mask(TOKEN)}")
print(f"  TELEGRAM_CHAT_ID   : {CHAT or '(not set)'}")
if TOKEN:
    if TOKEN != TOKEN.strip():
        problems.append("Token has leading/trailing whitespace in .env -- remove it.")
    if TOKEN[:1] in "\"'" or TOKEN[-1:] in "\"'":
        problems.append("Token is wrapped in quotes in .env -- .env needs no quotes.")
    if ":" not in TOKEN:
        problems.append("Token has no ':' -- it is not a Telegram token.")
    elif len(TOKEN.partition(":")[2]) < 30:
        problems.append("Token secret looks truncated -- copy the whole line from BotFather.")
else:
    problems.append("TELEGRAM_BOT_TOKEN is not set in .env.")

print()
print("=" * 72)
print("2. IS THE TOKEN VALID?   (getMe -- no chat needed)")
print("=" * 72)
token_ok = False
if TOKEN:
    try:
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getMe", timeout=10)
        if r.status_code == 200 and r.json().get("ok"):
            me = r.json()["result"]
            token_ok = True
            print(f"  VALID -- bot is @{me.get('username')} ({me.get('first_name')})")
        elif r.status_code == 401:
            print("  REJECTED (401) -- this token is dead.")
            print("  The bot was deleted, or the token was regenerated and .env still")
            print("  holds the old one. Open @BotFather -> /mybots -> your bot ->")
            print("  API Token -> copy it, or /revoke to mint a fresh one.")
            problems.append("Token rejected by Telegram -- get a current one from @BotFather.")
        else:
            print(f"  Unexpected {r.status_code}: {r.text[:200]}")
            problems.append(f"getMe returned {r.status_code}.")
    except requests.exceptions.RequestException as exc:
        print(f"  Could not reach Telegram: {exc}")
        problems.append("Network path to api.telegram.org is blocked from this box.")

print()
print("=" * 72)
print("3. WHICH CHATS HAVE MESSAGED THE BOT?   (getUpdates)")
print("=" * 72)
if token_ok:
    try:
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", timeout=10)
        ups = r.json().get("result", []) if r.status_code == 200 else []
        seen = {}
        for u in ups:
            msg = u.get("message") or u.get("channel_post") or {}
            chat = msg.get("chat") or {}
            if chat.get("id") is not None:
                seen[str(chat["id"])] = chat.get("username") or chat.get("title") or chat.get("first_name", "")
        if not seen:
            print("  No recent messages. Open Telegram, send your bot any message,")
            print("  then run this again -- the chat id only appears after you write to it.")
        for cid, who in seen.items():
            flag = "  <-- matches TELEGRAM_CHAT_ID" if cid == str(CHAT) else ""
            print(f"  chat id {cid:<16} {who}{flag}")
        if seen and str(CHAT) not in seen:
            problems.append(
                f"TELEGRAM_CHAT_ID is {CHAT}, which is not among the chats that have "
                f"messaged this bot ({', '.join(seen)}). Set .env to the right one."
            )
    except requests.exceptions.RequestException as exc:
        print(f"  Could not reach Telegram: {exc}")
else:
    print("  Skipped -- the token has to work first.")

print()
print("=" * 72)
print("4. SEND A REAL TEST MESSAGE")
print("=" * 72)
if token_ok and CHAT:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT, "text": "Guardian test message -- Telegram is working."},
            timeout=10,
        )
        if r.status_code == 200:
            print("  SENT -- check your phone. If it arrived, this path is fixed.")
        else:
            print(f"  FAILED ({r.status_code}): {r.text[:300]}")
            problems.append(f"sendMessage returned {r.status_code}.")
    except requests.exceptions.RequestException as exc:
        print(f"  FAILED: {exc}")
        problems.append("sendMessage could not reach Telegram.")
else:
    print("  Skipped -- needs a working token and a chat id.")

print()
print("=" * 72)
if problems:
    print(f"{len(problems)} PROBLEM(S) TO FIX")
    print("=" * 72)
    for i, p in enumerate(problems, 1):
        print(f"  {i}. {p}")
    sys.exit(1)
print("TELEGRAM IS WORKING")
print("=" * 72)
