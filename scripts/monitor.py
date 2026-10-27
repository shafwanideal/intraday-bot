"""Always-on monitor (run under PM2). Sends Telegram alerts for the things the guardians can't
report about themselves:

  * Kite login not done by 08:30 IST on a weekday (then a reminder every 10 minutes until 09:30),
    and "login received" once it is done.
  * The login listener (kite_login.py serve on 127.0.0.1:5099) going down / coming back up.
  * The guardians tmux session missing during market hours (09:00-15:35) / coming back up.

Alerts fire on a CHANGE of state, not on every check. State is in memory, so a restart of this
process can repeat one alert -- better than missing one. It does not know NSE holidays: on a
weekday holiday it will still remind you about the login; ignore it.

    .venv/bin/python3 scripts/monitor.py
"""
import subprocess
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import auth, telegram_notify  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
POLL_SECONDS = 30
LOGIN_DEADLINE = dtime(8, 30)
LOGIN_REMINDER_UNTIL = dtime(9, 30)
LOGIN_REMINDER_EVERY = 600  # seconds
GUARDIANS_WINDOW = (dtime(9, 0), dtime(15, 35))
LISTENER_HEALTH_URL = "http://127.0.0.1:5099/zerodha_callback_health"
FAILS_BEFORE_ALERT = 2  # consecutive failed checks before "down" (absorbs a blip / a restart)


def listener_ok() -> bool:
    try:
        return requests.get(LISTENER_HEALTH_URL, timeout=5).text.strip() == "ok"
    except requests.exceptions.RequestException:
        return False


def guardians_running() -> bool:
    try:
        return subprocess.run(["tmux", "has-session", "-t", "guardians"], capture_output=True).returncode == 0
    except OSError:
        return False


class Monitor:
    def __init__(self, send=telegram_notify.send_message, login_ok=auth.has_valid_session,
                 listener=listener_ok, guardians=guardians_running):
        self.send, self.login_ok, self.listener, self.guardians = send, login_ok, listener, guardians
        self.day = None
        self.login_done = False
        self.login_alert_at = 0.0
        self.login_alerted = False
        self.listener_fails = 0
        self.listener_down = False
        self.guardian_fails = 0
        self.guardians_down = False

    def _new_day(self, today) -> None:
        self.day, self.login_done, self.login_alerted, self.login_alert_at = today, False, False, 0.0

    def tick(self, now: datetime, mono: float) -> None:
        if self.day != now.date():
            self._new_day(now.date())
        weekday = now.weekday() < 5
        t = now.time()

        # --- login listener ---
        if self.listener():
            self.listener_fails = 0
            if self.listener_down:
                self.listener_down = False
                self.send("✅ Kite login listener is back up.")
        else:
            self.listener_fails += 1
            if self.listener_fails >= FAILS_BEFORE_ALERT and not self.listener_down:
                self.listener_down = True
                self.send("🔴 Kite login listener is DOWN — the login bookmark will not work until it is back (pm2 restart kite-login-callback).")

        # --- morning login ---
        if weekday and not self.login_done and t >= LOGIN_DEADLINE:
            if self.login_ok():
                self.login_done = True
                if self.login_alerted:
                    self.send("✅ Kite login received — thanks.")
            elif t < LOGIN_REMINDER_UNTIL and mono - self.login_alert_at >= LOGIN_REMINDER_EVERY:
                self.login_alert_at, self.login_alerted = mono, True
                self.send(f"⚠️ Kite login NOT done yet ({now:%H:%M}). Click the Kite login bookmark — the guardians and the bot need it.")

        # --- guardians session ---
        if weekday and GUARDIANS_WINDOW[0] <= t <= GUARDIANS_WINDOW[1]:
            if self.guardians():
                self.guardian_fails = 0
                if self.guardians_down:
                    self.guardians_down = False
                    self.send("✅ Guardians are back up.")
            else:
                self.guardian_fails += 1
                if self.guardian_fails >= FAILS_BEFORE_ALERT and not self.guardians_down:
                    self.guardians_down = True
                    self.send("🔴 Guardians are NOT running (no 'guardians' tmux session) during market hours. Positions are unprotected — log in via the bookmark to start them.")
        else:
            self.guardian_fails = 0


def main() -> None:
    mon = Monitor()
    if telegram_notify.enabled():
        telegram_notify.send_message("🟢 Monitoring started (login 08:30 check, listener, guardians).")
    else:
        print("WARNING: Telegram is not configured -- alerts will go nowhere.")
    while True:
        try:
            mon.tick(datetime.now(IST), time.monotonic())
        except Exception as exc:  # noqa: BLE001 - the monitor must outlive any single bad check
            print(f"monitor tick failed: {exc!r}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
