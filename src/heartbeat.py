"""Tiny liveness file both guardian loops touch every iteration, whether or
not the market is open. scripts/health_monitor.py (run independently by cron)
reads it to tell "process alive" from "loop actually ticking" -- a hung loop
stuck in a Kite call that never raises would still show up as a running
process in `pgrep`, but its heartbeat would stop moving.

Deliberately not a Telegram alert itself: writing a file can't fail in the
way a network call can, so the guardian loops keep this unconditional and
let the separate watchdog process decide what, if anything, is wrong.
"""

import json
from datetime import datetime
from pathlib import Path

HEARTBEAT_FILE = Path(__file__).resolve().parent.parent / "logs" / "heartbeat.json"


def touch(name: str) -> None:
    try:
        data = {}
        if HEARTBEAT_FILE.exists():
            data = json.loads(HEARTBEAT_FILE.read_text())
        data[name] = datetime.now().isoformat()
        HEARTBEAT_FILE.parent.mkdir(exist_ok=True)
        HEARTBEAT_FILE.write_text(json.dumps(data))
    except Exception:  # noqa: BLE001 - a heartbeat write must never take a guardian down
        pass
