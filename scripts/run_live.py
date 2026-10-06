import sys
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import telegram_notify  # noqa: E402
from src.live import LOG_DIR, run_live  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def main() -> None:
    # --protect-only: see run_live()'s own docstring for what this mode does
    # and why it's safe to run unattended. scripts/watchdog.py passes this
    # when restarting after a detected crash; a normal morning start never
    # does, so a human still always types CONFIRM for a fresh day's entries.
    protect_only = "--protect-only" in sys.argv[1:]
    try:
        run_live(protect_only=protect_only)
    except Exception:
        # 2026-10-06: a crash here used to just print a traceback to whatever
        # terminal happened to be attached and vanish once enough later output
        # scrolled past it -- real positions then sat unmonitored for hours
        # before anyone noticed, with no record of why it died. Now every
        # crash is both permanently logged and alerted immediately.
        now = datetime.now(IST)
        crash_log = LOG_DIR / f"crash_{now.date().isoformat()}.log"
        LOG_DIR.mkdir(exist_ok=True)
        with open(crash_log, "a") as f:
            f.write(f"\n--- crash at {now.isoformat()} (protect_only={protect_only}) ---\n")
            f.write(traceback.format_exc())
        telegram_notify.send_message(
            f"\U0001f6a8 Live trading crashed unexpectedly at {now.strftime('%H:%M:%S')} IST "
            f"(protect_only={protect_only}). Traceback saved to {crash_log}. "
            "Open positions may be unprotected until this is resolved."
        )
        raise


if __name__ == "__main__":
    main()
