"""Trailing stop guardian for Groww delivery holdings (alert-only by default).

    .venv/bin/python -m scripts.run_groww_holdings_guard --list   # show what it would guard, then exit
    .venv/bin/python -m scripts.run_groww_holdings_guard          # run during market hours
"""
import sys

from src import groww_client, groww_holdings_guard as m

if "--list" in sys.argv:
    g = groww_client.get_groww()
    h = m.fetch_free_holdings(g)
    print(f"Trail {m.TRAIL_PCT * 100:g}% ({m.TRAIL_MODE}) | live sells: {sorted(x for x in m.WATCH_SYMBOLS if m.live_sells_enabled(x)) or "none"}")
    if not h:
        print("Nothing guardable (no free quantity), or the fetch failed.")
    for sym, v in (h or {}).items():
        print(f"  {sym}: {v['qty']} free @ avg {v['avg']:.2f}")
else:
    m.run_guardian()
