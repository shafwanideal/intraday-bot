"""Independent health watchdog -- run every few minutes by cron, NOT as a
thread inside run_all_guardians.py. A hung guardian (stuck in a Kite call
that never raises, or a dead process) can't be trusted to report its own
failure, so this judges the guardians from outside, as a separate process.

What it checks on every run, during market hours only:
  - Kite session currently valid (auth.has_valid_session()) -- catches "not
    logged in yet" and "login happened but something stopped the running
    guardians from ever picking up the fresh token".
  - Both guardian loops have touched logs/heartbeat.json recently (proves
    each loop is actually ticking, not just that some python process with
    that name happens to exist).

What it does:
  - The first run at/after MORNING_CHECK_TIME each day sends one "bot is
    healthy" or "bot has a problem" message -- the requested 9 AM check-in.
  - Any run where a problem appears that wasn't there last run sends an
    immediate alert, so a mid-day failure (lost connection, a crashed
    process, a hung loop) is reported within one cron tick, not only at 9am.
  - When a reported problem clears, sends one "back to normal" message.

State persisted in .cache/health_status.json so separate cron invocations
(each a fresh process) share "already alerted" / "today's morning check
already sent" across runs.

Usage (crontab -e on the VPS):
    */5 9-15 * * 1-5 cd /home/trader/intraday-bot && .venv/bin/python3 scripts/health_monitor.py >> logs/health_monitor.log 2>&1
"""

import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import auth, telegram_notify  # noqa: E402
from src.heartbeat import HEARTBEAT_FILE  # noqa: E402

STATE_FILE = Path(__file__).resolve().parent.parent / ".cache" / "health_status.json"
MORNING_CHECK_TIME = time(9, 0)
STOP_CHECKING_AT = time(15, 35)
HEARTBEAT_STALE_AFTER = timedelta(minutes=5)
GUARDIANS = (("swing", "swing guardian"), ("intraday", "intraday guardian"))


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


def _heartbeat_age(name: str) -> timedelta | None:
    try:
        data = json.loads(HEARTBEAT_FILE.read_text())
        ts = datetime.fromisoformat(data[name])
        return datetime.now() - ts
    except Exception:
        return None


def diagnose() -> tuple[bool, list[str]]:
    problems = []
    if not auth.has_valid_session():
        problems.append("Kite session is NOT valid (not logged in today, or today's token is stale/rejected)")
    for key, label in GUARDIANS:
        age = _heartbeat_age(key)
        if age is None:
            problems.append(f"{label} heartbeat missing -- process may not be running")
        elif age > HEARTBEAT_STALE_AFTER:
            minutes = int(age.total_seconds() // 60)
            problems.append(f"{label} heartbeat is {minutes} min old -- loop may be hung or the process died")
    return (len(problems) == 0, problems)


def main() -> None:
    now_t = datetime.now().time()
    if now_t < MORNING_CHECK_TIME or now_t >= STOP_CHECKING_AT:
        return  # outside the trading window -- nothing to check or alert on

    today_str = date.today().isoformat()
    state = _load_state()
    if state.get("date") != today_str:
        state = {"date": today_str, "morning_sent": False, "problem_active": False}

    healthy, problems = diagnose()

    if not state["morning_sent"]:
        if healthy:
            telegram_notify.send_message("✅ 9 AM check: bot is logged in and both guardians are running fine.")
        else:
            telegram_notify.send_message("🔴 9 AM check found a problem:\n" + "\n".join(f"- {p}" for p in problems))
        state["morning_sent"] = True
        state["problem_active"] = not healthy
    elif healthy and state["problem_active"]:
        telegram_notify.send_message("✅ Bot is back to normal -- the earlier problem has cleared.")
        state["problem_active"] = False
    elif not healthy and not state["problem_active"]:
        telegram_notify.send_message("🔴 Bot problem detected:\n" + "\n".join(f"- {p}" for p in problems))
        state["problem_active"] = True

    _save_state(state)


if __name__ == "__main__":
    main()
