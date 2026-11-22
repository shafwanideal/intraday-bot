"""Send one test Telegram message. Prints only success/failure, never the token.
    .venv/bin/python -m scripts.test_telegram"""
from src import telegram_notify

if not telegram_notify.enabled():
    raise SystemExit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not both set in .env")
ok = telegram_notify.send_message("Test alert from your Contabo server. If you see this, Telegram alerts work.")
print("Sent. Check Telegram." if ok else "FAILED -- see the warning above (401 = the bot token is wrong or was revoked).")
