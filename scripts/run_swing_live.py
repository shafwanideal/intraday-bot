"""Run one daily swing-trading pass: resolve exits on open positions
automatically, propose new entries/averaging legs, then place whatever gets
CONFIRMed. See src/swing_live.py's own docstring for the full design.

Meant to run ONCE PER DAY, at or after market close (15:30 IST) -- e.g. via
cron/systemd timer, or by hand. Unlike scripts/run_live.py this does NOT
poll continuously; swing positions are evaluated off that day's final close,
per src/swing_strategy.py's own design.

Usage:
    .venv/bin/python3 scripts/run_swing_live.py

Manage the watchlist (symbols eligible for a brand-new entry) with
scripts/swing_add.py, or edit swing_watchlist.json directly.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import telegram_notify  # noqa: E402
from src.swing_live import run_swing_check  # noqa: E402


def main() -> None:
    run_swing_check(notifier=telegram_notify.send_message if telegram_notify.enabled() else None)


if __name__ == "__main__":
    main()
