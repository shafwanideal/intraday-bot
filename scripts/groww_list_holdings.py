"""Read-only: list the CNC (delivery) holdings the Groww API returns, i.e. what a
holdings guardian would see. Places NO orders.

    .venv/bin/python -m scripts.groww_list_holdings
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.groww_client import get_groww  # noqa: E402

g = get_groww()
print("Logged in.")
resp = g.get_holdings_for_user()
rows = resp.get("holdings", []) if isinstance(resp, dict) else resp
if not rows:
    raise SystemExit("No holdings returned.")

print(f"\nFields on each holding: {sorted(rows[0].keys())}\n")


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


total = 0.0
print(f"{'SYMBOL':<14}{'QTY':>7}{'FREE':>7}{'PLEDGE':>8}{'GRW_LCK':>9}{'T1':>5}{'AVG':>10}{'COST':>11}  NOTE")
for h in sorted(rows, key=lambda r: -num(r.get("quantity")) * num(r.get("average_price"))):
    q, avg = num(h.get("quantity")), num(h.get("average_price"))
    if q <= 0:
        continue
    total += q * avg
    free = num(h.get("demat_free_quantity"))
    pledge = num(h.get("pledge_quantity")) + num(h.get("repledge_quantity"))
    glock = num(h.get("groww_locked_quantity")) + num(h.get("demat_locked_quantity"))
    t1 = num(h.get("t1_quantity"))
    note = []
    if pledge or glock:
        note.append("PLEDGED/LOCKED (likely MTF)")
    if t1:
        note.append("T+1 not delivered yet")
    if q and free < q and not (pledge or glock or t1):
        note.append("check")
    print(f"{h.get('trading_symbol', '?'):<14}{q:>7.0f}{free:>7.0f}{pledge:>8.0f}{glock:>9.0f}{t1:>5.0f}{avg:>10.2f}{q * avg:>11,.0f}  {'; '.join(note)}")
print(f"\nTotal cost of holdings: {total:,.0f}")
