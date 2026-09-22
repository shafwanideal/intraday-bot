"""One command for the whole morning routine: pull latest code, log into
Kite, take today's picks, then go straight to live trading.

This does not remove any safety gate -- LIVE_TRADING_ENABLED is still set
explicitly (and turned back off after the run), and run_live.py still
requires a typed CONFIRM against the real sizing before placing any order.
This script only collapses the boilerplate around that gate -- git pull,
nano-editing the plan file, nano-editing .env -- into one command.

Pass today's picks as a single quoted argument:

    .venv/bin/python3 scripts/daily.py "RVNL long 40%, MAZDOCK short 40%, GRANULES long 20%"

or run it with no argument and it will ask for them instead.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import auth  # noqa: E402
from src.live import TODAYS_STOCKS_FILE as LIVE_PLAN_FILE  # noqa: E402
from src.telegram_control import _describe_entry, parse_picks, validate_plan_consistency  # noqa: E402

ENV_FILE = PROJECT_ROOT / ".env"


def set_live_trading_enabled(value: bool) -> None:
    """Add or update the LIVE_TRADING_ENABLED line in .env in place.

    Also sets it directly in THIS process's os.environ, not just the file --
    run_live.py's own config.py calls load_dotenv() with the library default
    (override=False), which only fills in variables NOT already present in
    os.environ. Since this process already imported src.live/src.config above
    (which ran load_dotenv() and cached whatever the file said BEFORE this
    function's edit -- almost always "false", left over from the previous
    day's run), the subprocess.run() below would otherwise inherit that
    stale cached value from this process's environment and never see the
    fresh "true" just written to the file. Real bug, 2026-09-22: this exact
    mismatch made run_live.py raise "LIVE_TRADING_ENABLED is not set to
    'true'" immediately after this function had, in fact, just set it.
    """
    new_line = f"LIVE_TRADING_ENABLED={'true' if value else 'false'}"
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    for i, line in enumerate(lines):
        if line.strip().startswith("LIVE_TRADING_ENABLED="):
            lines[i] = new_line
            break
    else:
        lines.append(new_line)
    ENV_FILE.write_text("\n".join(lines) + "\n")
    os.environ["LIVE_TRADING_ENABLED"] = "true" if value else "false"


def main() -> None:
    print("=" * 70)
    print("DAILY STARTUP -- LIVE TRADING")
    print("=" * 70)

    print("\n1. Pulling latest code...")
    subprocess.run(["git", "pull", "origin", "main-47itwf"], cwd=PROJECT_ROOT)

    print("\n2. Kite login...")
    if auth.has_valid_session():
        print("Already logged in for today.")
    else:
        auth.login(paste_only=True)

    text = " ".join(sys.argv[1:]).strip()
    if not text:
        print("\n3. Enter today's picks, e.g.:")
        print("   RVNL long 40%, MAZDOCK short 40%, GRANULES long 20%")
        text = input("> ")
    else:
        print(f"\n3. Today's picks: {text}")

    try:
        plan = parse_picks(text)
        validate_plan_consistency(plan)
    except ValueError as exc:
        print(f"\nCouldn't understand that plan: {exc}")
        print("Nothing written, nothing started. Run this again to retry.")
        return

    print("\nParsed plan:")
    for sym, entry in plan.items():
        print(f"  {sym:12s} {_describe_entry(entry)}")

    LIVE_PLAN_FILE.write_text(json.dumps(plan, indent=2))
    set_live_trading_enabled(True)
    print(f"\nPlan written to {LIVE_PLAN_FILE}. LIVE_TRADING_ENABLED set to true.")
    print("Starting scripts/run_live.py -- you'll still be asked to type CONFIRM before anything real trades.\n")
    try:
        subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "run_live.py")], cwd=PROJECT_ROOT)
    finally:
        set_live_trading_enabled(False)
        print("\nLIVE_TRADING_ENABLED turned back off.")


if __name__ == "__main__":
    main()
