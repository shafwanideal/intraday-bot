"""One-share test: can Groww's API accept a SELL for an MTF position?

Places ONE limit SELL of 1 share ~15% above the current price (it should not
fill), prints Groww's answer, then cancels it straight away. Real order on
your real account -- requires --go.

    .venv/bin/python -m scripts.test_groww_mtf_sell --go [SYMBOL]

SYMBOL defaults to the open MTF position with the lowest share price.
"""
import sys
from datetime import time

from src import groww_client, groww_swing_live, yahoo_intraday
from src.swing_live import IST, _now

if "--go" not in sys.argv:
    raise SystemExit("This places a real (far-from-market) order and cancels it. Re-run with --go.")

now = _now()
if now.weekday() >= 5 or not (time(9, 20) <= now.time() < time(15, 20)):
    raise SystemExit(f"Market is not open for this test (now {now:%a %H:%M} IST). Run 9:20-15:20 Mon-Fri.")

g = groww_client.get_groww()
positions = groww_swing_live.fetch_mtf_positions(g)
if not positions:
    raise SystemExit("No open MTF positions found (or fetch failed).")
args = [a for a in sys.argv[1:] if not a.startswith("--")]
symbol = args[0] if args else min(positions, key=lambda s: positions[s]["average_price"])
if symbol not in positions:
    raise SystemExit(f"{symbol} is not an open MTF position: {list(positions)}")

df = yahoo_intraday.fetch_intraday(symbol, period="1d", interval="1m")
if df.empty or (now - df.index[-1].to_pydatetime()).total_seconds() > 600:
    raise SystemExit("No fresh Yahoo price for " + symbol + "; not placing anything.")
ltp = float(df["Close"].iloc[-1])
price = round(round(ltp * 1.15 / 0.05) * 0.05, 2)
print(f"Testing {symbol}: last price {ltp}, placing 1-share LIMIT SELL (product MTF) at {price}")

order_id = None
try:
    resp = g.place_order(
        trading_symbol=symbol, quantity=1, price=price, validity=g.VALIDITY_DAY,
        exchange=g.EXCHANGE_NSE, segment=g.SEGMENT_CASH, product=g.PRODUCT_MTF,
        order_type=g.ORDER_TYPE_LIMIT, transaction_type=g.TRANSACTION_TYPE_SELL,
    )
    print("PLACE RESPONSE:", resp)
    order_id = resp.get("groww_order_id")
except Exception as exc:  # noqa: BLE001
    print(f"\nRESULT: REJECTED -> {exc!r}")
    print("Groww's API does NOT accept this MTF sell as sent. The guardian's live sells will fail.")
    raise SystemExit(1)
finally:
    if order_id:
        try:
            print("STATUS BEFORE CANCEL:", g.get_order_status(segment=g.SEGMENT_CASH, groww_order_id=order_id))
        except Exception as exc:  # noqa: BLE001
            print("status check failed:", repr(exc))
        try:
            print("CANCEL RESPONSE:", g.cancel_order(groww_order_id=order_id, segment=g.SEGMENT_CASH))
        except Exception as exc:  # noqa: BLE001
            print(f"!!! CANCEL FAILED ({exc!r}) -- cancel order {order_id} in the Groww app NOW !!!")

print("\nRESULT: ACCEPTED -> Groww's API takes an MTF SELL. Check the Groww orders screen to confirm it shows cancelled.")
