"""Watchdog for scripts/run_live.py -- added 2026-10-06 after a live session
crashed silently at 09:16 AM and left real open positions with ZERO
trailing-stop/loss-cap/profit-target monitoring for about 5 hours before
anyone noticed (no error logged either -- an unhandled exception's traceback
only ever went to whatever terminal was attached that moment).

Runs continuously (meant to be installed as a systemd service, same pattern
as deploy/intraday-bot.service). Every POLL_SECONDS, checks: did today's
session start (a real CONFIRM happened) but never reach a clean end? If so
and no run_live.py process is currently alive, something killed it
unexpectedly -- restart it in protect_only mode (see run_live()'s docstring)
and alert immediately over Telegram. protect_only is what makes this safe to
automate unattended: it can only ever place protective EXIT orders on
positions that already exist, never place a new entry a human hasn't
reviewed, and needs no interactive CONFIRM for exactly that reason.

Caps restarts at MAX_RESTARTS_PER_DAY so a persistently-crashing bug can't
turn into an infinite restart loop silently hiding the real problem -- past
that, it alerts once and stops touching the process, same philosophy as
deploy/intraday-bot.service's StartLimitBurst.
"""

import json
import subprocess
import sys
import time as time_module
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import telegram_notify  # noqa: E402
from src.live import LOG_DIR, PREOPEN_ENTRY_START  # noqa: E402
from src.strategy import SQUARE_OFF_TIME  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOT_PY = PROJECT_ROOT / ".venv" / "bin" / "python3"
POLL_SECONDS = 20
MAX_RESTARTS_PER_DAY = 5
# A session's log only ever reaches one of these once it's genuinely done for
# the day -- "start" without either of these after it means something ended
# the process some other way (a crash, a kill) partway through.
TERMINAL_KINDS = {"end", "confirmation_declined"}


def _today_log_path(today: date) -> Path:
    return LOG_DIR / f"live_{today.isoformat()}.jsonl"


def _session_should_be_running(today: date) -> bool:
    path = _today_log_path(today)
    if not path.exists():
        return False
    started = False
    ended = False
    for line in path.read_text().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = entry.get("kind")
        if kind == "start":
            started = True
        elif kind in TERMINAL_KINDS:
            ended = True
    return started and not ended


def _run_live_alive() -> bool:
    result = subprocess.run(["pgrep", "-f", "scripts/run_live.py"], capture_output=True, text=True)
    return bool(result.stdout.strip())


def main() -> None:
    restarts_today = 0
    tracked_date: date | None = None
    print(f"watchdog started, polling every {POLL_SECONDS}s")

    while True:
        now = datetime.now(IST)
        today = now.date()
        if today != tracked_date:
            restarts_today = 0
            tracked_date = today

        in_session_window = PREOPEN_ENTRY_START <= now.time() < SQUARE_OFF_TIME
        if in_session_window and _session_should_be_running(today) and not _run_live_alive():
            if restarts_today >= MAX_RESTARTS_PER_DAY:
                telegram_notify.send_message(
                    f"\U0001f6a8 run_live.py has crashed again and the watchdog already used its "
                    f"{MAX_RESTARTS_PER_DAY} restart attempts for today. NOT restarting again -- "
                    "this needs manual attention on the VPS right now."
                )
                time_module.sleep(POLL_SECONDS)
                continue

            restarts_today += 1
            telegram_notify.send_message(
                f"⚠️ run_live.py is not running at {now.strftime('%H:%M:%S')} IST but today's "
                f"session never cleanly ended -- it crashed. Restarting in protect-only mode "
                f"(attempt {restarts_today}/{MAX_RESTARTS_PER_DAY}): resumes trailing-stop/loss-cap/"
                "profit-target on existing positions, places NO new entries."
            )
            crash_log = LOG_DIR / f"crash_{today.isoformat()}.log"
            LOG_DIR.mkdir(exist_ok=True)
            with open(crash_log, "a") as f:
                f.write(f"\n--- watchdog restart attempt {restarts_today} at {now.isoformat()} ---\n")
                f.flush()
                subprocess.Popen(
                    [str(BOT_PY), "scripts/run_live.py", "--protect-only"],
                    cwd=PROJECT_ROOT,
                    stdout=f,
                    stderr=subprocess.STDOUT,
                )

            time_module.sleep(10)  # give it a moment to come up before checking
            if _run_live_alive():
                telegram_notify.send_message(
                    "Protect-only session is back up -- existing positions have trailing-stop/"
                    "loss-cap/profit-target protection again."
                )
            else:
                telegram_notify.send_message(
                    f"Restart attempt did not leave a process running -- check {crash_log} and "
                    "the VPS directly now."
                )

        time_module.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
