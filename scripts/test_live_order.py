import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import auth, config, orders

CONFIRM_PHRASE = "I CONFIRM LIVE TRADING WITH REAL MONEY"
TEST_SYMBOL = "JYOTICNC"
TEST_QUANTITY = 1  # smallest possible real order, just to validate the pipeline end to end


def main() -> None:
    if not config.LIVE_TRADING_ENABLED:
        print("LIVE_TRADING_ENABLED is not set to 'true' in .env. Add it explicitly to proceed.")
        sys.exit(1)

    kite = auth.get_kite()

    print("=" * 70)
    print("LIVE ORDER PIPELINE TEST -- THIS WILL PLACE A REAL ORDER WITH REAL MONEY")
    print("=" * 70)
    print(f"Symbol: {TEST_SYMBOL}   Quantity: {TEST_QUANTITY} share(s)   Product: MIS (intraday)")
    print("Plan: BUY 1 share, confirm the fill, then immediately SELL it back to close out.")
    print("This validates the order-placement pipeline end to end -- it is NOT a strategy test.")
    print()
    typed = input(f'Type exactly "{CONFIRM_PHRASE}" to proceed, anything else aborts: ')
    if typed.strip() != CONFIRM_PHRASE:
        print("Confirmation did not match -- aborting. No orders placed.")
        return

    print(f"\nPlacing BUY order: {TEST_QUANTITY} share(s) of {TEST_SYMBOL}, MIS market order...")
    try:
        buy_order_id = orders.place_market_order(kite, TEST_SYMBOL, "BUY", TEST_QUANTITY, tag="pipeline_test_buy")
    except orders.OrderPlacementAmbiguous as exc:
        print(f"\nCRITICAL: {exc}")
        print("Check Kite directly RIGHT NOW to see if this order actually went through.")
        return

    print(f"Buy order placed: {buy_order_id}. Waiting for fill...")
    buy_result = orders.wait_for_fill(kite, buy_order_id)
    print(f"Buy result: {buy_result['status']}, avg_price={buy_result['average_price']}, filled_qty={buy_result['filled_quantity']}")

    if buy_result["status"] != "COMPLETE":
        print("\nBuy order did NOT confirm as COMPLETE. Check Kite directly before doing anything else.")
        print(f"Full result: {buy_result}")
        return

    print("\nBuy confirmed. Immediately closing out with a SELL order...")
    try:
        sell_order_id = orders.place_market_order(
            kite, TEST_SYMBOL, "SELL", buy_result["filled_quantity"], tag="pipeline_test_sell"
        )
    except orders.OrderPlacementAmbiguous as exc:
        print(f"\nCRITICAL: {exc}")
        print(
            f"You have an OPEN real position: {buy_result['filled_quantity']} share(s) of {TEST_SYMBOL} "
            f"bought at {buy_result['average_price']}. Check Kite directly and close this manually if needed."
        )
        return

    print(f"Sell order placed: {sell_order_id}. Waiting for fill...")
    sell_result = orders.wait_for_fill(kite, sell_order_id)
    print(f"Sell result: {sell_result['status']}, avg_price={sell_result['average_price']}, filled_qty={sell_result['filled_quantity']}")

    if sell_result["status"] != "COMPLETE":
        print("\nSell order did NOT confirm as COMPLETE. You may have an OPEN real position.")
        print(f"Check Kite directly RIGHT NOW. Full result: {sell_result}")
        return

    gross_pnl = (sell_result["average_price"] - buy_result["average_price"]) * buy_result["filled_quantity"]
    print(
        f"\nRound trip complete. Buy @ {buy_result['average_price']}, Sell @ {sell_result['average_price']}, "
        f"qty={buy_result['filled_quantity']}, gross P&L: Rs {gross_pnl:.2f} (before brokerage/STT/other charges)"
    )
    print("\nPipeline validated: auth -> real order placement -> fill confirmation -> round trip all worked.")


if __name__ == "__main__":
    main()
