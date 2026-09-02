import time as time_module
from decimal import ROUND_HALF_UP, Decimal

import requests
from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException

from . import kite_data

FILL_TIMEOUT_SECONDS = 30
FILL_POLL_INTERVAL_SECONDS = 1.0
TERMINAL_STATUSES = {"COMPLETE", "REJECTED", "CANCELLED"}

# Kite's API rejects plain MARKET orders outright ("Market orders without
# market protection are not allowed via API"). The standard workaround --
# and arguably better practice anyway, since it caps worst-case slippage --
# is a LIMIT order priced a small buffer beyond the reference price, wide
# enough to fill immediately against the current bid/ask like a market
# order would, but capped so a freak price spike can't fill at an
# arbitrarily bad price.
LIMIT_PRICE_BUFFER_PCT = 0.005  # 0.5%

# A network-level error (timeout, connection reset) never reached Kite's
# servers in any confirmable way -- these are the genuinely ambiguous ones.
# A KiteException (InputException, PermissionException, etc, including the
# confusingly-named NetworkException) is only ever raised by kiteconnect
# after it received and parsed an actual error response FROM Kite -- the
# request definitely arrived and was definitely rejected for a known reason.
# That's a clean failure, not an ambiguous one; treating it as ambiguous
# (see OrderPlacementAmbiguous below) would halt an entire session over one
# stock-specific rejection that has nothing to do with the other symbols.
AMBIGUOUS_EXCEPTIONS = (requests.exceptions.RequestException,)
PLACEMENT_EXCEPTIONS = (KiteException, requests.exceptions.RequestException)  # used where the distinction doesn't matter (polling reads)


def _protected_limit_price(reference_price: float, transaction_type: str, tick_size: float) -> float:
    """Kite rejects any LIMIT price that isn't an exact multiple of the
    instrument's tick size (commonly 0.05, but not guaranteed) -- round to
    the nearest valid tick after applying the buffer.

    Uses Decimal rather than plain float arithmetic: binary floats can't
    exactly represent values like 906.3, so a naive float round-to-tick can
    produce a price that's off by a tiny fraction of a paisa -- harmless to
    a human eye, but real money, and not worth risking a rejection (or
    worse, a silently-accepted-but-wrong price) over avoidable imprecision.
    """
    buffer = Decimal(str(LIMIT_PRICE_BUFFER_PCT))
    multiplier = Decimal("1") + (buffer if transaction_type == "BUY" else -buffer)
    ref = Decimal(str(reference_price))
    tick = Decimal(str(tick_size))
    buffered = ref * multiplier
    ticks = (buffered / tick).to_integral_value(rounding=ROUND_HALF_UP)
    return float(ticks * tick)


class OrderRejected(Exception):
    """Kite received the order request and definitively rejected it (bad
    params, MIS blocked for this instrument, insufficient margin, etc). Not
    ambiguous -- we know for certain no order was placed. Safe to just log
    and move on to other symbols; no need to halt the whole session over
    one stock-specific rejection."""


class OrderPlacementAmbiguous(Exception):
    """The placement request itself failed at the network level (timeout,
    connection reset) and we could not confirm via kite.orders() whether it
    actually reached the exchange anyway. This is a real-money unknown
    state -- do NOT retry placing the same order (the original request may
    have succeeded despite the error response never reaching us, so
    retrying risks a duplicate real order). Caller must halt and surface
    this for a human to check Kite directly."""


def place_market_order(
    kite: KiteConnect, symbol: str, transaction_type: str, quantity: int, reference_price: float, tag: str | None = None
) -> str:
    """Place a real MIS (intraday) order on NSE that behaves like a market
    order -- fills immediately at the best available price -- but is
    actually a LIMIT order priced LIMIT_PRICE_BUFFER_PCT beyond
    `reference_price` (the current LTP or similar), since Kite's API
    rejects plain market orders outright. Returns the order_id.

    transaction_type must be "BUY" or "SELL". A returned order_id does NOT
    mean the order filled, only that Kite accepted the request; use
    wait_for_fill() to confirm the actual outcome.

    Raises OrderRejected if Kite cleanly rejected the request (a definite,
    known outcome -- e.g. MIS blocked for this instrument) -- caller should
    just skip this symbol, not halt everything. Raises
    OrderPlacementAmbiguous only for a genuine network-level failure, where
    it's unclear whether the request reached Kite at all; does NOT retry in
    that case (checks kite.orders() for a possible match first, since
    blindly retrying risks a duplicate real order).

    Raises ValueError up front if `tag` is over Kite's 20-char limit, rather
    than letting Kite reject the order -- found for real on 2026-09-02, where
    tag="portfolio_profit_lock" (22 chars) caused every single position's
    exit order to get rejected simultaneously when the portfolio profit lock
    fired, leaving all 7 real positions open and completely unmonitored
    (the engine's own bookkeeping had already assumed they were closed).
    """
    if tag is not None and len(tag) > 20:
        raise ValueError(f"Order tag {tag!r} is {len(tag)} chars, over Kite's 20-char limit -- Kite would reject this order.")
    tick_size = kite_data.get_tick_size(kite, symbol)
    limit_price = _protected_limit_price(reference_price, transaction_type, tick_size)
    try:
        return kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NSE,
            tradingsymbol=symbol,
            transaction_type=transaction_type,
            quantity=quantity,
            product=kite.PRODUCT_MIS,
            order_type=kite.ORDER_TYPE_LIMIT,
            price=limit_price,
            tag=tag,
        )
    except KiteException as exc:
        raise OrderRejected(f"place_order({symbol}, {transaction_type}, qty={quantity}) was rejected by Kite: {exc}") from exc
    except AMBIGUOUS_EXCEPTIONS as exc:
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
    "raw": <last order history entry>}.

    On timeout, actively cancels the order rather than leaving it resting on
    the exchange -- an unfilled LIMIT order left open (e.g. GROWW on
    2026-08-26, ATHERENERG/TEJASNET on 2026-08-28, all thin/illiquid at that
    moment) can fill later completely outside the engine's tracking, since no
    position was ever recorded for it. Re-checks order history once after the
    cancel attempt: if it turns out the order actually filled in the brief
    window between the last poll and the cancel call, that COMPLETE result is
    returned instead of a misleading TIMEOUT. If genuinely nothing can be
    confirmed (e.g. the cancel call itself failed for an unclear reason),
    status is "TIMEOUT" -- caller must treat this as "unknown, needs manual
    checking", not as success or failure, and check Kite directly.
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

    try:
        kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=order_id)
    except PLACEMENT_EXCEPTIONS:
        pass  # may already be filled/cancelled/rejected -- the re-check below is what matters

    try:
        history = kite.order_history(order_id)
        if history:
            final_entry = history[-1]
            final_status = final_entry.get("status")
            if final_status in TERMINAL_STATUSES:
                return {
                    "status": final_status,
                    "average_price": final_entry.get("average_price") or None,
                    "filled_quantity": final_entry.get("filled_quantity") or 0,
                    "raw": final_entry,
                }
    except PLACEMENT_EXCEPTIONS:
        pass

    return {
        "status": "TIMEOUT",
        "average_price": last_entry.get("average_price") if last_entry else None,
        "filled_quantity": last_entry.get("filled_quantity") if last_entry else 0,
        "raw": last_entry,
    }
