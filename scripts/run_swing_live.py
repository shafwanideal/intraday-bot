"""Continuously track real Zerodha MTF (leveraged swing) positions and
auto-exit on a trailing-stop breach. See src/swing_live.py's own docstring
for the full design -- in short: entries/averaging are placed MANUALLY by
the trader outside this codebase; this process only ever watches and sells,
never buys, which is what makes it safe to run unattended with no CONFIRM
prompt.

Meant to run CONTINUOUSLY during market hours (9:15-15:30 IST), not once a
day -- start it in a tmux session each morning (or install it as a systemd
service, same pattern as deploy/watchdog.service, for true unattended
operation) and leave it running.

Usage:
    .venv/bin/python3 scripts/run_swing_live.py
    .venv/bin/python3 scripts/run_swing_live.py --poll-interval 5

--poll-interval SECONDS overrides the default 30s. Safe well below that --
each poll is one quote() call (batched across every tracked symbol, so this
doesn't scale with how many positions you're watching) plus one positions()
call; quote() is Kite's only 1 req/sec-limited endpoint here, so anything
down to a couple of seconds stays comfortably under it. Faster polling only
narrows the window between a real trailing-stop breach and this noticing it
-- it does not turn the check into a resting broker-side order, so a sharp
gap between two polls can still fill worse than the stop level either way.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import telegram_notify  # noqa: E402
from src.swing_live import POLL_INTERVAL_SECONDS, run_swing_guardian  # noqa: E402


def main() -> None:
    poll_interval = POLL_INTERVAL_SECONDS
    if "--poll-interval" in sys.argv:
        idx = sys.argv.index("--poll-interval")
        try:
            poll_interval = int(sys.argv[idx + 1])
        except (IndexError, ValueError):
            print("Usage: --poll-interval SECONDS (a whole number)")
            sys.exit(1)
    run_swing_guardian(
        poll_interval=poll_interval, notifier=telegram_notify.send_message if telegram_notify.enabled() else None
    )


if __name__ == "__main__":
    main()
