"""Print exactly what Kite reports we hold, and what the guardians track.

Written after four live MTF positions vanished from swing_live's tracking at
09:15 on 2026-10-27. The guardians read kite.positions()["net"] only -- an
MTF position that has settled into HOLDINGS is legitimately absent from that
endpoint, which looks identical to "it was sold". This prints both books plus
the on-disk guardian state so the three can be compared directly.

    .venv/bin/python3 scripts/show_positions.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import auth, orders  # noqa: E402

kite = auth.get_kite(interactive=sys.stdin.isatty())

print("=" * 78)
print("POSITIONS  -- kite.positions()['net']   (what the guardians read)")
print("=" * 78)
net = kite.positions().get("net", [])
rows = [p for p in net if p.get("quantity", 0) != 0]
if not rows:
    print("  (nothing with a non-zero quantity)")
for p in rows:
    mtf = "  <-- MTF, guardian tracks this" if p.get("product") == orders.PRODUCT_MTF else ""
    print(f"  {p['tradingsymbol']:<14}{p.get('product',''):<6}qty {p['quantity']:>6}"
          f"  avg {p.get('average_price', 0):>10,.2f}{mtf}")

print()
print("=" * 78)
print("HOLDINGS   -- kite.holdings()           (NOT read by any guardian)")
print("=" * 78)
try:
    hold = kite.holdings()
    print(f"  {len(hold)} rows returned")
    for h in hold:
        # MTF quantity is NOT in h["quantity"] -- it sits in a separate
        # field (Kite's UI shows it as "M: 30"). Print every quantity-ish
        # field so the real one is unambiguous.
        qty_fields = {k: v for k, v in h.items()
                      if "quantity" in k.lower() or k.lower() == "mtf"}
        print(f"  {h['tradingsymbol']:<13} avg {h.get('average_price', 0):>9,.2f}"
              f"  ltp {h.get('last_price', 0):>9,.2f}")
        print(f"      {qty_fields}")
    if hold:
        print()
        print("  --- ALL KEYS on the first holding, for reference ---")
        print(f"  {sorted(hold[0].keys())}")
except Exception as exc:  # noqa: BLE001
    print(f"  could not fetch holdings: {exc}")

print()
print("=" * 78)
print("GUARDIAN STATE -- what is actually being tracked right now")
print("=" * 78)
root = Path(__file__).resolve().parent.parent
for name in ("swing_positions_state.json", "intraday_positions_state.json"):
    f = root / name
    if not f.exists():
        print(f"  {name}: (no file)")
        continue
    try:
        data = json.loads(f.read_text())
    except Exception as exc:  # noqa: BLE001
        print(f"  {name}: unreadable ({exc})")
        continue
    print(f"  {name}: {list(data) or '(empty)'}")
    for sym, st in data.items():
        print(f"      {sym:<12} entry {st.get('entry_date','?')}  "
              f"trailing {st.get('trailing')}  peak {st.get('peak_price')}  "
              f"funded {st.get('funded_amount')}  missing_polls {st.get('missing_polls', 0)}")
