"""Groww swing (MTF) trailing-stop guardian. ALERT-ONLY unless
GROWW_LIVE_ORDERS=true. See src/groww_swing_live.py.

    .venv/bin/python -m scripts.run_groww_swing --list   # show what it sees, then exit
    .venv/bin/python -m scripts.run_groww_swing          # run during market hours
"""
import sys

from src import groww_client, groww_swing_live

if "--list" in sys.argv:
    g = groww_client.get_groww()
    pos = groww_swing_live.fetch_mtf_positions(g)
    print("Open MTF positions the guardian would track:" if pos else "None found (or fetch failed).")
    for sym, p in (pos or {}).items():
        print(f"  {sym}: qty {p['quantity']} @ {p['average_price']}")
else:
    groww_swing_live.run_guardian()
