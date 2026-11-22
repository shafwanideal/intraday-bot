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
print(f"{'SYMBOL':<14}{'QTY':>8}{'AVG':>11}{'COST':>13}")
for h in sorted(rows, key=lambda r: -num(r.get("quantity")) * num(r.get("average_price"))):
    q, avg = num(h.get("quantity")), num(h.get("average_price"))
    if q <= 0:
        continue
    total += q * avg
    print(f"{h.get('trading_symbol', '?'):<14}{q:>8.0f}{avg:>11.2f}{q * avg:>13,.0f}")
print(f"\nTotal cost of holdings: {total:,.0f}")
