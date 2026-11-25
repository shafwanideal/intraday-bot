"""Standalone entrypoint for the daily backup stop-loss GTT.

Deliberately its OWN process, in its own terminal/tmux pane -- never merged
into scripts/run_all_guardians.py or scripts/run_pdf_entry_bot.py. Explicit
requirement (2026-11-25): this must never touch or risk Part 1 (the
exit-only guardians) or Part 2 (the pick/auto-adopt bot) -- it only adds a
single-leg stop-loss GTT underneath any MTF holding that has no GTT at all
(see src/crash_backup_gtt.py's own docstring for exactly why single-leg
keeps it invisible to Part 1's own GTT-detection).

Usage (run alongside the other two, in a separate pane):
    .venv/bin/python3 scripts/run_crash_backup_gtt.py

Requires CRASH_BACKUP_GTT_ENABLED=true in .env -- a separate gate from both
LIVE_TRADING_ENABLED and PDF_ENTRY_TRADING_ENABLED.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import auth, telegram_notify  # noqa: E402
from src.crash_backup_gtt import run_crash_backup_gtt  # noqa: E402


def main() -> None:
    auth.get_kite(interactive=sys.stdin.isatty())
    notifier = telegram_notify.send_message if telegram_notify.enabled() else None
    if notifier:
        notifier("✅ Daily backup stop-loss bot is running (separate from the exit-only guardians and Part 2).")
    print("Crash backup GTT bot running on its own. Ctrl+C stops it; nothing else is touched by this.", flush=True)
    try:
        run_crash_backup_gtt(notifier=notifier)
    except KeyboardInterrupt:
        print("\nStopping -- finishing the current check, then exiting.")


if __name__ == "__main__":
    main()
