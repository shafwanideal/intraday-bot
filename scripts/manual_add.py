"""One-off manual order placement, gated behind the same real-money CONFIRM
prompt as live.py -- for adding to an existing position (or any other
one-off real order) outside the bot's own automated entry/exit logic,
without bypassing the safety gate that every other order today has gone
through.

Usage:
    python3 scripts/manual_add.py SYMBOL BUY|SELL QUANTITY

Places a real MIS market order (via src.orders.place_market_order, same
mechanism live.py uses), waits for fill confirmation, and prints the result.
Does NOT touch todays_live_stocks.json or any running live.py session's
in-memory state -- if a live.py session is currently tracking this symbol,
restart it afterward so it reconciles the new blended quantity/avg price.
"""

import sys

from src import auth, orders


def main() -> None:
    if len(sys.argv) != 4:
        print(f"Usage: python3 {sys.argv[0]} SYMBOL BUY|SELL QUANTITY")
        sys.exit(1)

    symbol = sys.argv[1].upper()
    transaction_type = sys.argv[2].upper()
    if transaction_type not in ("BUY", "SELL"):
        print(f"transaction_type must be BUY or SELL, got: {transaction_type}")
        sys.exit(1)
    quantity = int(sys.argv[3])

    kite = auth.get_kite()
    ltp = kite.ltp(f"NSE:{symbol}")[f"NSE:{symbol}"]["last_price"]

    print("=" * 70)
    print("MANUAL ORDER -- THIS WILL PLACE A REAL ORDER WITH REAL MONEY")
    print("=" * 70)
    print(f"{transaction_type} {quantity} {symbol} @ ~Rs {ltp:,.2f} (current LTP, market-like limit order)")
    print(f"Approx order value: Rs {quantity * ltp:,.2f}")
    raw = input('Type exactly "CONFIRM" to proceed, anything else aborts: ')
    if raw.strip() != "CONFIRM":
        print("Aborted -- no order placed.")
        sys.exit(0)

    order_id = orders.place_market_order(kite, symbol, transaction_type, quantity, ltp, tag="manual_add")
    print(f"Order placed: {order_id}")
    fill = orders.wait_for_fill(kite, order_id)
    print(f"Fill result: {fill}")
    if fill["status"] == "COMPLETE":
        print(
            f"\nFILLED: {transaction_type} {fill['filled_quantity']} {symbol} @ Rs {fill['average_price']:,.2f}\n"
            "If a live.py session is tracking this symbol, restart it now so it reconciles the new position."
        )
    else:
        print(f"\nNOT fully filled -- status: {fill['status']}. Check Kite directly.")


if __name__ == "__main__":
    main()
