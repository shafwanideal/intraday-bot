import time as time_module

from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException

FILL_TIMEOUT_SECONDS = 30
FILL_POLL_INTERVAL_SECONDS = 1.0
TERMINAL_STATUSES = {"COMPLETE", "REJECTED", "CANCELLED"}


def place_market_order(kite: KiteConnect, symbol: str, transaction_type: str, quantity: int, tag: str | None = None) -> str:
    """Place a real MIS (intraday) market order on NSE. Returns the order_id.

    transaction_type must be "BUY" or "SELL". Raises KiteException on
    placement failure (network error, invalid params, etc.) -- a raised
    order_id here does NOT mean the order filled, only that Kite accepted
    the request; use wait_for_fill() to confirm the actual outcome.
    """
    return kite.place_order(
        variety=kite.VARIETY_REGULAR,
        exchange=kite.EXCHANGE_NSE,
        tradingsymbol=symbol,
        transaction_type=transaction_type,
        quantity=quantity,
        product=kite.PRODUCT_MIS,
        order_type=kite.ORDER_TYPE_MARKET,
        tag=tag,
    )


def wait_for_fill(
    kite: KiteConnect,
    order_id: str,
    timeout_seconds: float = FILL_TIMEOUT_SECONDS,
    poll_interval: float = FILL_POLL_INTERVAL_SECONDS,
) -> dict:
    """Poll order history until it reaches a terminal state (COMPLETE,
    REJECTED, or CANCELLED), or the timeout elapses.

    Returns {"status": ..., "average_price": float|None, "filled_quantity": int,
    "raw": <last order history entry>}. On timeout without a terminal state,
    status is "TIMEOUT" -- caller must treat this as "unknown, needs manual
    checking", not as success or failure.
    """
    deadline = time_module.monotonic() + timeout_seconds
    last_entry: dict | None = None

    while time_module.monotonic() < deadline:
        try:
            history = kite.order_history(order_id)
        except KiteException:
            time_module.sleep(poll_interval)
            continue

        if history:
            last_entry = history[-1]
            status = last_entry.get("status")
            if status in TERMINAL_STATUSES:
                return {
                    "status": status,
                    "average_price": last_entry.get("average_price") or None,
                    "filled_quantity": last_entry.get("filled_quantity") or 0,
                    "raw": last_entry,
                }
        time_module.sleep(poll_interval)

    return {
        "status": "TIMEOUT",
        "average_price": last_entry.get("average_price") if last_entry else None,
        "filled_quantity": last_entry.get("filled_quantity") if last_entry else 0,
        "raw": last_entry,
    }
