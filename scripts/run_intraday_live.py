"""Continuously track MANUALLY-placed intraday (MIS) trades -- ones placed
on impulse, outside scripts/daily.py's plan -- and apply the original
trailing-SL rule (1%/0.5% arm before/after 11:00 IST, 0.75% trail), plus a
hard square-off by 15:08 IST since MIS can never carry overnight. See
src/intraday_live.py's own docstring for the full design.

Only ever places an EXIT, never an entry -- safe to run unattended with no
CONFIRM prompt, same reasoning as the swing guardian.

IMPORTANT: do not run this at the same time scripts/run_live.py is managing
the SAME symbol -- this guardian already skips anything in today's
todays_live_stocks.json plan file to reduce that overlap, but if you add a
symbol to that plan file WHILE this is already tracking it manually, you
could still end up with two independent watchers on one real position.

Usage:
    .venv/bin/python3 scripts/run_intraday_live.py
    .venv/bin/python3 scripts/run_intraday_live.py --poll-interval 5
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import telegram_notify  # noqa: E402
from src.intraday_live import POLL_INTERVAL_SECONDS, run_intraday_guardian  # noqa: E402


def main() -> None:
    poll_interval = POLL_INTERVAL_SECONDS
    if "--poll-interval" in sys.argv:
        idx = sys.argv.index("--poll-interval")
        try:
            poll_interval = int(sys.argv[idx + 1])
        except (IndexError, ValueError):
            print("Usage: --poll-interval SECONDS (a whole number)")
            sys.exit(1)
    run_intraday_guardian(
        poll_interval=poll_interval, notifier=telegram_notify.send_message if telegram_notify.enabled() else None
    )


if __name__ == "__main__":
    main()
