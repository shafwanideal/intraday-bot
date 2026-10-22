"""Real-money test: can Groww's API BUY then SELL 1 share as CNC (delivery)?

Buys exactly ONE share of a cheap NSE stock, then sells that one share.
Costs a few rupees in charges. It does NOT touch your MTF positions.

    .venv/bin/python -m scripts.test_groww_cnc_roundtrip --go SYMBOL

SYMBOL is required (pick a liquid NSE stock under the price cap). Flags:
    --max-price 20   refuse if the stock trades above this (default 20)
Run 9:25-14:45 IST Mon-Fri. Never places more than one buy and one sell,
and cancels anything still open when it exits.
"""
import sys
import time as time_module
from datetime import time

from src import groww_client, yahoo_intraday
from src.swing_live import _now

FILLED = {"EXECUTED", "DELIVERY_AWAITED", "COMPLETED"}
DEAD = {"FAILED", "REJECTED", "CANCELLED"}


def tick(price: float) -> float:
    return round(round(price / 0.05) * 0.05, 2)


def wait_final(g, order_id, seconds=60):
    status = None
    for _ in range(seconds // 3):
        time_module.sleep(3)
        try:
            status = g.get_order_status(segment=g.SEGMENT_CASH, groww_order_id=order_id)
        except Exception as exc:  # noqa: BLE001
            print("  status check failed:", repr(exc))
            continue
        s = str(status.get("order_status", "")).upper()
        print("  status:", s)
        if s in FILLED or s in DEAD:
            break
    return status


def place(g, symbol, side, price):
    return g.place_order(
        trading_symbol=symbol, quantity=1, price=price, validity=g.VALIDITY_DAY,
        exchange=g.EXCHANGE_NSE, segment=g.SEGMENT_CASH, product=g.PRODUCT_CNC,
        order_type=g.ORDER_TYPE_LIMIT,
        transaction_type=g.TRANSACTION_TYPE_BUY if side == "BUY" else g.TRANSACTION_TYPE_SELL,
    )


def leg(g, symbol, side, price):
    print(f"\n{side} 1 {symbol} CNC limit @ {price}")
    try:
        resp = place(g, symbol, side, price)
    except Exception as exc:  # noqa: BLE001
        print(f"  {side} REJECTED: {exc!r}")
        return False
    print("  response:", resp)
    order_id = resp.get("groww_order_id")
    if not order_id or str(resp.get("order_status", "")).upper() in DEAD:
        print(f"  {side} NOT PLACED: status={resp.get('order_status')} remark={resp.get('remark')!r}")
        return False
    final = wait_final(g, order_id)
    s = str((final or {}).get("order_status", "")).upper()
    if s in FILLED:
        print(f"  {side} FILLED")
        return True
    print(f"  {side} not filled (status {s or 'unknown'}) -- cancelling")
    try:
        print("  cancel:", g.cancel_order(groww_order_id=order_id, segment=g.SEGMENT_CASH))
    except Exception as exc:  # noqa: BLE001
        print(f"  !!! CANCEL FAILED ({exc!r}) -- cancel order {order_id} in the Groww app NOW !!!")
    return False


if "--go" not in sys.argv:
    raise SystemExit("This buys 1 real share and sells it. Re-run with --go SYMBOL.")
args = [a for a in sys.argv[1:] if not a.startswith("--")]
max_price = 20.0
if "--max-price" in sys.argv:
    max_price = float(sys.argv[sys.argv.index("--max-price") + 1])
    args = [a for a in args if a != str(sys.argv[sys.argv.index("--max-price") + 1])]
if len(args) != 1:
    raise SystemExit("Give exactly one SYMBOL, e.g. --go SYMBOL")
symbol = args[0].upper()

now = _now()
if now.weekday() >= 5 or not (time(9, 25) <= now.time() < time(14, 45)):
    raise SystemExit(f"Run 9:25-14:45 IST Mon-Fri (now {now:%a %H:%M}).")

df = yahoo_intraday.fetch_intraday(symbol, period="1d", interval="1m")
if df.empty or (now - df.index[-1].to_pydatetime()).total_seconds() > 600:
    raise SystemExit(f"No fresh Yahoo price for {symbol}; not placing anything.")
ltp = float(df["Close"].iloc[-1])
if ltp > max_price:
    raise SystemExit(f"{symbol} trades at {ltp:.2f}, above the {max_price} cap; not placing anything.")
print(f"{symbol}: last price {ltp:.2f}")

g = groww_client.get_groww()
if not leg(g, symbol, "BUY", tick(ltp * 1.01)):
    raise SystemExit("\nRESULT: BUY did not complete -- nothing to sell. See messages above.")

sell_price = tick(ltp * 0.99)
if leg(g, symbol, "SELL", sell_price):
    print("\nRESULT: SUCCESS -- Groww's API can BUY and SELL a CNC delivery share.")
else:
    print(f"\nRESULT: BUY worked but SELL did not. You may still hold 1 share of {symbol}.")
    print("If the reason was same-day/settlement, sell that 1 share in the Groww app, or re-run tomorrow.")
