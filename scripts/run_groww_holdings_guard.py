"""Trailing stop guardian for Groww delivery holdings (alert-only by default).

    .venv/bin/python -m scripts.run_groww_holdings_guard --list   # show what it would guard, then exit
    .venv/bin/python -m scripts.run_groww_holdings_guard          # run during market hours
"""
import sys

from src import groww_client, groww_holdings_guard as m

if "--status" in sys.argv:
    g = groww_client.get_groww()
    h = m.fetch_free_holdings(g)
    state = m.load_state()
    if not h:
        raise SystemExit("Nothing guardable right now (no free quantity), or the fetch failed.")

    class _Q:  # fetch_prices only logs stale Yahoo prices through this
        def event(self, *a, **k):
            pass
    prices, source = m.fetch_prices(list(h), _Q())
    print(f"Prices from {source}. Trail {m.TRAIL_PCT * 100:g} pts, tightens to {m.TIGHT_TRAIL * 100:g} pts once a peak return reaches +{m.TIGHT_ARM * 100:g}%.")
    print(f"{'SYMBOL':<11}{'QTY':>6}{'AVG':>9}{'PRICE':>9}{'RETURN':>8}{'PEAK':>9}{'STOP':>9}{'TO STOP':>9}  MODE")
    for sym, v in h.items():
        px = prices.get(sym)
        st = state.get(sym) or {}
        peak = max(st.get("peak") or 0, px or 0)
        if not px or not peak:
            print(f"{sym:<11}{v['qty']:>6}{v['avg']:>9.2f}  (no price yet)")
            continue
        stop = m.stop_price(peak, v["avg"])
        mode = "LIVE SELL" if m.live_sells_enabled(sym) else "alert only"
        print(f"{sym:<11}{v['qty']:>6}{v['avg']:>9.2f}{px:>9.2f}{(px / v['avg'] - 1) * 100:>7.1f}%{peak:>9.2f}{stop:>9.2f}{(px / stop - 1) * 100:>8.1f}%  {mode}")
elif "--list" in sys.argv:
    g = groww_client.get_groww()
    h = m.fetch_free_holdings(g)
    print(f"Trail {m.TRAIL_PCT * 100:g}% ({m.TRAIL_MODE}) | live sells: {sorted(x for x in m.WATCH_SYMBOLS if m.live_sells_enabled(x)) or "none"}")
    if not h:
        print("Nothing guardable (no free quantity), or the fetch failed.")
    for sym, v in (h or {}).items():
        print(f"  {sym}: {v['qty']} free @ avg {v['avg']:.2f}")
else:
    m.run_guardian()
