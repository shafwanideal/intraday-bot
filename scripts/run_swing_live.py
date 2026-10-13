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
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import telegram_notify  # noqa: E402
from src.swing_live import run_swing_guardian  # noqa: E402


def main() -> None:
    run_swing_guardian(notifier=telegram_notify.send_message if telegram_notify.enabled() else None)


if __name__ == "__main__":
    main()
