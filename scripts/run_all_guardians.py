"""Single entrypoint for the daily routine: one login, one terminal, both
guardians (swing/MTF and intraday/manual MIS) running together.

Replaces running scripts/run_swing_live.py and scripts/run_intraday_live.py
separately in two tmux sessions with two separate logins -- the trader
explicitly asked for the daily login to stop being a two-session, two-login
chore. Both guardians share the same Kite account, so logging in once here
(before either guardian starts) means neither guardian's own auth.get_kite()
call below has to prompt again -- it just finds today's session already
cached.

Usage:
    .venv/bin/python3 scripts/run_all_guardians.py
"""

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import auth, telegram_notify  # noqa: E402
from src.digest import run_digest_loop  # noqa: E402
from src.intraday_live import POLL_INTERVAL_SECONDS as INTRADAY_POLL_INTERVAL_SECONDS  # noqa: E402
from src.intraday_live import run_intraday_guardian  # noqa: E402
from src.swing_live import POLL_INTERVAL_SECONDS as SWING_POLL_INTERVAL_SECONDS  # noqa: E402
from src.swing_live import run_swing_guardian  # noqa: E402


def main() -> None:
    # Reads today's token from .env (written when you click the Kite login bookmark) and
    # RAISES with a clear 'log in via the bookmark' message if it's missing or stale --
    # the bot never starts a login itself, so nothing can sit blocked on input() under
    # tmux/systemd and silently never start the guardians.
    auth.get_kite(interactive=sys.stdin.isatty())

    notifier = telegram_notify.send_message if telegram_notify.enabled() else None

    # Tell the phone we're up -- as "back up" if the supervisor flagged a crash/outage first.
    down_marker = Path(__file__).resolve().parent.parent / ".cache" / "guardians_down"
    if notifier:
        was_down = down_marker.exists()
        notifier("✅ Guardians are BACK UP (swing + intraday) after going down." if was_down
                 else "✅ Guardians are running (swing + intraday).")
    down_marker.unlink(missing_ok=True)

    swing_thread = threading.Thread(
        target=run_swing_guardian,
        kwargs={"poll_interval": SWING_POLL_INTERVAL_SECONDS, "notifier": notifier},
        name="swing-guardian",
        daemon=True,
    )
    intraday_thread = threading.Thread(
        target=run_intraday_guardian,
        kwargs={"poll_interval": INTRADAY_POLL_INTERVAL_SECONDS, "notifier": notifier},
        name="intraday-guardian",
        daemon=True,
    )

    digest_thread = threading.Thread(
        target=run_digest_loop,
        kwargs={"get_kite": auth.get_kite, "notifier": notifier},
        name="digest",
        daemon=True,
    )

    swing_thread.start()
    intraday_thread.start()
    digest_thread.start()

    print("Both guardians (and the digest) are running. Ctrl+C stops all.", flush=True)
    try:
        # Plain .join() would block the main thread uninterruptibly on POSIX
        # once both targets are daemon threads sleeping in time.sleep(); a
        # timeout loop keeps the main thread waking up regularly so Ctrl+C
        # (SIGINT) actually gets delivered and handled promptly.
        while swing_thread.is_alive() or intraday_thread.is_alive() or digest_thread.is_alive():
            swing_thread.join(timeout=1)
            intraday_thread.join(timeout=1)
            digest_thread.join(timeout=1)
    except KeyboardInterrupt:
        print("\nStopping -- each guardian finishes its current poll, then this process exits.")


if __name__ == "__main__":
    main()
