"""One-share test: does Groww's API accept a CNC SELL of a FREE delivery holding?

Places ONE limit SELL of 1 share a few % ABOVE the current price (so it should
not fill), prints Groww's answer, then cancels it. Real order on your real
account -- requires --go. Run 9:20-15:15 IST Mon-Fri.

    .venv/bin/python -m scripts.test_groww_cnc_holding_sell --go SYMBOL [--pct 5]

SYMBOL must have free (demat) quantity -- see scripts.groww_list_holdings.
"""
import sys
from datetime import time

from src import groww_client, yahoo_intraday
from src.swing_live import _now

DEAD = {"FAILED", "REJECTED", "CANCELLED"}

if "--go" not in sys.argv:
    raise SystemExit("This places a real (far-from-market) order and cancels it. Re-run with --go SYMBOL.")
pct = 5.0
argv = sys.argv[1:]
if "--pct" in argv:
    i = argv.index("--pct")
    pct = float(argv[i + 1])
    del argv[i:i + 2]
args = [a for a in argv if not a.startswith("--")]
if len(args) != 1:
    raise SystemExit("Give exactly one SYMBOL.")
symbol = args[0].upper()

now = _now()
if now.weekday() >= 5 or not (time(9, 20) <= now.time() < time(15, 15)):
    raise SystemExit(f"Run 9:20-15:15 IST Mon-Fri (now {now:%a %H:%M}).")

g = groww_client.get_groww()
resp = g.get_holdings_for_user()
rows = resp.get("holdings", []) if isinstance(resp, dict) else resp
h = next((r for r in rows if r.get("trading_symbol") == symbol), None)
if h is None:
    raise SystemExit(f"{symbol} is not in your holdings.")
free = float(h.get("demat_free_quantity") or 0)
if free < 1:
    raise SystemExit(f"{symbol} has no free (demat) quantity ({free:g}); pick one with FREE > 0.")

df = yahoo_intraday.fetch_intraday(symbol, period="1d", interval="1m")
if df.empty or (now - df.index[-1].to_pydatetime()).total_seconds() > 1800:
    raise SystemExit(f"No recent Yahoo price for {symbol}; not placing anything.")
ltp = float(df["Close"].iloc[-1])
price = round(round(ltp * (1 + pct / 100) / 0.05) * 0.05, 2)
print(f"{symbol}: last price {ltp:.2f}; placing 1-share CNC LIMIT SELL at {price} (+{pct:g}%)")

order_id = None
try:
    r = g.place_order(
        trading_symbol=symbol, quantity=1, price=price, validity=g.VALIDITY_DAY,
        exchange=g.EXCHANGE_NSE, segment=g.SEGMENT_CASH, product=g.PRODUCT_CNC,
        order_type=g.ORDER_TYPE_LIMIT, transaction_type=g.TRANSACTION_TYPE_SELL,
    )
    print("PLACE RESPONSE:", r)
    order_id = r.get("groww_order_id")
    if not order_id or str(r.get("order_status", "")).upper() in DEAD:
        print(f"\nRESULT: NOT ACCEPTED -> status={r.get('order_status')} remark={r.get('remark')!r}")
        raise SystemExit(1)
finally:
    if order_id:
        try:
            print("STATUS:", g.get_order_status(segment=g.SEGMENT_CASH, groww_order_id=order_id))
            print("CANCEL:", g.cancel_order(groww_order_id=order_id, segment=g.SEGMENT_CASH))
        except Exception as exc:  # noqa: BLE001
            print(f"!!! CANCEL FAILED ({exc!r}) -- cancel order {order_id} in the Groww app NOW !!!")

print("\nRESULT: ACCEPTED -> Groww's API takes a CNC SELL of a free holding. Confirm in the Groww app that it shows cancelled.")
