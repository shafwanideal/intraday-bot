"""Standalone entrypoint for Part 2: the PDF/pick entry-automation bot.

Deliberately its OWN process, in its own terminal/tmux pane -- never merged
into scripts/run_all_guardians.py's thread list. That's not a style choice:
it's the explicit requirement (2026-11-21) that this new add-on must never
touch, risk, or even share a process with the existing exit-only guardians
(swing + intraday + digest + hourly P&L + Nifty50 scanner), which keep
running completely unaffected via run_all_guardians.py exactly as before.

Usage (run alongside run_all_guardians.py, in a separate pane):
    .venv/bin/python3 scripts/run_pdf_entry_bot.py

Requires PDF_ENTRY_TRADING_ENABLED=true in .env (a separate gate from
LIVE_TRADING_ENABLED -- see src/pdf_entry_live.py's own docstring for why).

This bot never buys -- the trader places every MTF buy himself, from the PDF.
To hand it a pick to watch, write a JSON list to logs/pending_picks.json,
e.g.:
    [{"symbol": "EXIDEIND", "sl": 388, "t1": 412, "t2": 426}]
It's consumed and the file cleared within one poll (POLL_INTERVAL_SECONDS).
Once his MTF buy for that symbol shows up in kite.positions(), the bot
splits it 50/50 and places the two exit GTTs automatically.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import auth, telegram_notify  # noqa: E402
from src.pdf_entry_live import run_pdf_entry_bot  # noqa: E402


def main() -> None:
    auth.get_kite(interactive=sys.stdin.isatty())
    notifier = telegram_notify.send_message if telegram_notify.enabled() else None
    if notifier:
        notifier("✅ PDF entry bot is running (part 2 -- separate from the exit-only guardians).")
    print("PDF entry bot running on its own. Ctrl+C stops it; the exit-only guardians are untouched by this.", flush=True)
    try:
        run_pdf_entry_bot(notifier=notifier)
    except KeyboardInterrupt:
        print("\nStopping -- finishing the current poll, then exiting.")


if __name__ == "__main__":
    main()
