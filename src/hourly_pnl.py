"""Hourly Telegram P&L update, sent once per clock hour during market hours
(10:00 through 15:00 IST) -- independent of and in addition to the 4:01 PM
end-of-day table in src/digest.py, which is untouched by this module.

The explicit ask this exists for: a running total of today's realized P&L
and which stocks were sold, INCLUDING trades the trader books manually
(partial profit-taking, a manual stop-loss) outside the guardians' own
trailing-stop exits -- not just bot-triggered ones.

Why this needs its own FIFO matching rather than reusing swing_exit/
intraday_exit log events: those events only exist for exits the GUARDIANS
themselves placed. A manual sell/cover via the Kite app or website never
logs one of those -- the guardian only ever notices the position is gone
and logs "...position_closed_externally" with no P&L, because it never saw
the fill price. kite.orders() is the fix: it returns the FULL order book
for the account for the day, regardless of which client placed an order
(this API, the Kite app, the Kite website), so FIFO-matching today's
COMPLETE orders per symbol, the same technique already proven in
src/live.py's _fetch_realized_pnl_today for MIS (real incidents fixed
there: a symbol closed and reopened same day, kite.margins()'
m2m_realised reading back wrong), correctly captures BOTH bot-triggered
and manual trades uniformly, for BOTH MIS and MTF.

The one gap plain FIFO-over-today's-orders has: a genuinely carried-over
swing (MTF) position's original BUY happened on a PREVIOUS day, so it has
no "today's order" to match its close against. src/swing_live.py's own
MTF_COST_BASIS_FILE -- a continuously-updated cache of each MTF symbol's
last known avg_price/qty, not a once-a-day snapshot (see its own docstring
for why: this system gets restarted mid-session often, and a point-in-time
snapshot would miss a trade closed before that restart) -- supplies that
missing opening leg, but ONLY for a symbol with no BUY order of its own
today: if a symbol was ALSO bought today (restart aside, a trader averaging
into an existing carried position on the very day they close it), the cache
is skipped and the close is matched only against today's own orders, which
under-counts the pre-existing carried lot's P&L -- a known, accepted
limitation of this simpler approach, not something to silently get wrong
the other way (double-counting) by seeding on top of a same-day buy too.
"""

import json
import time as time_module
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from . import orders
from .swing_live import MTF_COST_BASIS_FILE

IST = ZoneInfo("Asia/Kolkata")
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

HOURLY_LOG_FILE = LOG_DIR / "hourly_pnl.log"
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
# Fire at the top of each of these hours -- not 09:xx (too early, essentially
# never anything closed yet) and the 4:01 PM table already covers end of day.
REPORT_HOURS = (10, 11, 12, 13, 14, 15)

TRACKED_PRODUCTS = ("MIS", orders.PRODUCT_MTF)


def _log(line: str) -> None:
    stamped = f"{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')} {line}"
    print(stamped, flush=True)
    try:
        HOURLY_LOG_FILE.parent.mkdir(exist_ok=True)
        with open(HOURLY_LOG_FILE, "a") as f:
            f.write(stamped + "\n")
    except OSError:
        pass


def _signed_rupees(amount: float) -> str:
    sign = "+" if amount >= 0 else "-"
    return f"{sign}Rs {abs(amount):,.2f}"


def _load_mtf_cost_basis_cache() -> dict[str, dict]:
    try:
        return json.loads(MTF_COST_BASIS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _fifo_realized(orders_today: list[dict], seed: tuple[float, int] | None) -> tuple[float, int]:
    """FIFO-match one symbol's today's COMPLETE orders (chronological order),
    optionally seeded with a pre-existing (avg_price, qty) lot carried in
    from before today. Returns (realized_pnl, quantity_closed).

    Same matching rule as src/live.py's _reconstruct_symbol_state (kept as an
    independent copy rather than imported -- that function feeds real-money
    loss-cap/profit-lock decisions and must never be touched for a
    reporting-only change): a lot on the OPPOSITE side closes against the
    front of the queue; same side appends a new lot."""
    queue: list[list] = []
    if seed and seed[1] > 0:
        queue.append([seed[0], seed[1], "BUY"])  # MTF is long-only; a carried-in seed is always a BUY lot
    realized = 0.0
    closed_qty = 0
    for o in sorted(orders_today, key=lambda o: o.get("order_timestamp") or ""):
        side = o["transaction_type"]
        qty = o["filled_quantity"]
        price = o["average_price"]
        while qty > 0:
            if queue and queue[0][2] != side:
                lot_price, lot_qty, lot_side = queue[0]
                matched = min(qty, lot_qty)
                realized += (price - lot_price) * matched if lot_side == "BUY" else (lot_price - price) * matched
                closed_qty += matched
                lot_qty -= matched
                qty -= matched
                if lot_qty == 0:
                    queue.pop(0)
                else:
                    queue[0][1] = lot_qty
            else:
                queue.append([price, qty, side])
                qty = 0
    return realized, closed_qty


def build_hourly_report(kite) -> str | None:
    """Everything realized (closed) so far TODAY, recomputed fresh each call
    -- cumulative for the whole day, not just since the last report, so each
    hour's message stands alone. Returns None if nothing has closed yet (no
    message sent that hour)."""
    try:
        all_orders = kite.orders()
    except orders.PLACEMENT_EXCEPTIONS as exc:
        _log(f"could not fetch orders ({exc!r}) -- skipping this hour's report")
        return None

    by_symbol_product: dict[tuple[str, str], list[dict]] = {}
    for o in all_orders:
        if o.get("status") != "COMPLETE" or o.get("product") not in TRACKED_PRODUCTS:
            continue
        by_symbol_product.setdefault((o["tradingsymbol"], o["product"]), []).append(o)

    cost_basis_cache = _load_mtf_cost_basis_cache()

    rows: list[tuple[str, str, int, float]] = []
    total = 0.0
    for (symbol, product), order_list in sorted(by_symbol_product.items()):
        seed = None
        # Only seed from the cache when there's no BUY of this symbol in today's
        # own orders -- if there IS one, the position was (at least partly) opened
        # today and matching today's orders alone is correct; seeding on top of
        # that too would double-count the same-day buy. See this module's own
        # docstring for the resulting known limitation (a same-day top-up of an
        # existing carried position).
        if product == orders.PRODUCT_MTF and not any(o["transaction_type"] == "BUY" for o in order_list):
            carried = cost_basis_cache.get(symbol)
            if carried:
                seed = (carried["average_price"], carried["quantity"])
        realized, closed_qty = _fifo_realized(order_list, seed)
        if closed_qty == 0:
            continue  # opened today (or still open from before), nothing actually closed
        total += realized
        rows.append((symbol, product, closed_qty, realized))

    if not rows:
        return None

    now_str = datetime.now(IST).strftime("%H:%M")
    lines = [
        f"Hourly P&L update ({now_str} IST)",
        "",
        f"Today's realized P&L so far: {_signed_rupees(total)}",
        "",
        "Sold today (includes trades booked manually, not just bot exits):",
    ]
    for symbol, product, qty, pnl in sorted(rows, key=lambda r: -r[3]):
        emoji = "✅" if pnl >= 0 else "\U0001F53B"  # checkmark / red triangle down
        lines.append(f"{emoji} {symbol} ({product}) qty {qty}: {_signed_rupees(pnl)}")

    return "\n".join(lines)


def run_hourly_pnl_loop(get_kite, notifier, should_stop=None, poll_interval: int = 30) -> None:
    """Fires once per REPORT_HOURS hour, cumulative for the day so far. Never
    raises: any failure is logged and the loop just tries again next poll --
    a notification outage here must never affect trading."""
    _log("hourly P&L thread started")
    sent_hours: set[tuple[date, int]] = set()
    while True:
        if should_stop is not None and should_stop():
            _log("hourly P&L thread stopping (should_stop)")
            return
        now = datetime.now(IST)
        if (
            MARKET_OPEN <= now.time() <= MARKET_CLOSE
            and now.hour in REPORT_HOURS
            and now.minute < 2
            and (now.date(), now.hour) not in sent_hours
        ):
            sent_hours.add((now.date(), now.hour))
            try:
                kite = get_kite()
                message = build_hourly_report(kite)
            except Exception as exc:  # noqa: BLE001 - one bad hour must never kill this thread
                _log(f"failed to build hourly report: {exc!r}")
                message = None
            if message is None:
                _log(f"{now.hour}:00 report: nothing sold yet today -- no message sent")
            elif notifier is None:
                _log(f"{now.hour}:00 report built but no notifier configured -- not sent")
            else:
                ok = notifier(message)
                _log(f"{now.hour}:00 report {'sent' if ok else 'FAILED to send'}")
        time_module.sleep(poll_interval)
