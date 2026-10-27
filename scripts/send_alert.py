"""Send one Telegram message from the command line (used by the supervisor script).

    .venv/bin/python3 scripts/send_alert.py "text"      # exit 0 if sent, 1 if not
    .venv/bin/python3 scripts/send_alert.py --test      # sends a test message
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import telegram_notify  # noqa: E402

if __name__ == "__main__":
    text = "✅ Test alert from the trading bot." if sys.argv[1:] == ["--test"] else " ".join(sys.argv[1:]).strip()
    if not text:
        sys.exit("usage: send_alert.py <text> | --test")
    ok = telegram_notify.send_message(text)
    print("sent" if ok else "NOT sent (Telegram not configured or rejected the token)")
    sys.exit(0 if ok else 1)
