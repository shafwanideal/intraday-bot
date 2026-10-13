"""Add one or more symbols to the swing-trading watchlist
(swing_watchlist.json) -- candidates for a brand-new swing (CNC) entry on
the next run of scripts/run_swing_live.py. Does NOT place any order itself;
it only stages the symbol for consideration. No direction field -- the
strategy is long-only (see src/swing_strategy.py) -- and no expiry: a
symbol stays on the list until it's entered or removed.

Usage:
    python3 scripts/swing_add.py SYMBOL [SYMBOL ...]
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.swing_live import SWING_WATCHLIST_FILE, load_watchlist  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: python3 {sys.argv[0]} SYMBOL [SYMBOL ...]")
        sys.exit(1)
    symbols = [s.upper() for s in sys.argv[1:]]
    watchlist = load_watchlist()
    added = [s for s in symbols if s not in watchlist]
    watchlist.extend(added)
    with open(SWING_WATCHLIST_FILE, "w") as f:
        json.dump(watchlist, f, indent=2)
    if added:
        print(f"Added to swing watchlist: {', '.join(added)}")
    already = sorted(set(symbols) - set(added))
    if already:
        print(f"Already on watchlist (unchanged): {', '.join(already)}")
    print(f"Full watchlist: {watchlist}")


if __name__ == "__main__":
    main()
