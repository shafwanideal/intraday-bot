import time as time_module

import requests
from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException

FILL_TIMEOUT_SECONDS = 30
FILL_POLL_INTERVAL_SECONDS = 1.0
TERMINAL_STATUSES = {"COMPLETE", "REJECTED", "CANCELLED"}

# A network error during any Kite call (not just a clean rejection) --
# doesn't tell us whether the request actually reached Kite's servers.
PLACEMENT_EXCEPTIONS = (KiteException, requests.exceptions.RequestException)


class OrderPlacementAmbiguous(Exception):
    """place_market_order() failed and we could not confirm via kite.orders()
    whether the order actually reached the exchange. This is a real-money
    unknown state -- do NOT retry placing the same order (the original
    request may have succeeded despite the error response never reaching
    us, so retrying risks a duplicate real order). Caller must halt and
    surface this for a human to check Kite directly."""


def place_market_order(kite: KiteConnect, symbol: str, transaction_type: str, quantity: int, tag: str | None = None) -> str:
    """Place a real MIS (intraday) market order on NSE. Returns the order_id.

    transaction_type must be "BUY" or "SELL". A returned order_id does NOT
    mean the order filled, only that Kite accepted the request; use
    wait_for_fill() to confirm the actual outcome.

    If the placement call itself fails (network error, timeout), this does
    NOT retry -- a network error doesn't tell us whether the original
    request reached Kite before failing, and blindly retrying risks placing
    a real duplicate order. Instead it checks kite.orders() for a matching
    order that may have gone through anyway; if none is found, raises
    OrderPlacementAmbiguous rather than guessing.
    """
    try:
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
    except PLACEMENT_EXCEPTIONS as exc:
        order_id = _find_recent_matching_order(kite, symbol, transaction_type, quantity, tag)
        if order_id:
            return order_id
        raise OrderPlacementAmbiguous(
            f"place_order({symbol}, {transaction_type}, qty={quantity}) failed ({exc!r}) and no matching "
            "order was found in kite.orders() -- whether this reached the exchange is unknown. "
            "Check Kite directly before placing anything else for this symbol."
        ) from exc


def _find_recent_matching_order(
    kite: KiteConnect, symbol: str, transaction_type: str, quantity: int, tag: str | None
) -> str | None:
    """Best-effort check for an order that may have been placed despite the
    placement call failing on our end. Not a substitute for checking Kite
    directly -- just enough to avoid an obviously-wrong duplicate retry."""
    try:
        all_orders = kite.orders()
    except PLACEMENT_EXCEPTIONS:
        return None
    for o in reversed(all_orders):
        if (
            o.get("tradingsymbol") == symbol
            and o.get("transaction_type") == transaction_type
            and o.get("quantity") == quantity
            and (tag is None or o.get("tag") == tag)
        ):
            return o.get("order_id")
    return None


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
        except PLACEMENT_EXCEPTIONS:
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
