"""Live MONITORING (not entry) for the swing/positional strategy
(src/swing_strategy.py) on real Zerodha MTF (leveraged) positions.

Entries and averaging legs are placed MANUALLY by the trader, outside this
codebase -- this module never places a BUY. Its only job is to watch
whatever MTF positions actually exist on Kite, apply the same profit-
target-arm / trailing-stop rules as the backtest engine, and place the
matching real SELL the moment a trailing stop is breached.

That one-directional capability is what makes this safe to run
UNATTENDED, continuously, with no CONFIRM gate at all -- same safety
argument as run_live.py's protect_only mode: it can only ever close a
position that's already open, never commit fresh capital. There is
nothing here for a human to approve.

Position discovery and sizing both come straight from Kite
(kite.positions()["net"], product="MTF") on every poll, not from a file
this code maintains:
- The trader's own average_price is always the blended cost Kite itself
  tracks, so a manual averaging leg is picked up automatically on the next
  poll -- no leg bookkeeping needed here at all (contrast with the earlier
  CNC design, which tracked individual legs because it placed the orders
  itself).
- A manual partial or full sell is picked up the same way: this module
  always reads current quantity fresh from Kite at the moment it places an
  exit, never from a cached value.

The only state THIS module needs to persist across polls/restarts is the
trailing-arm flag and peak price per symbol (SWING_STATE_FILE) -- Kite has
no concept of "has this crossed our profit-target-arm threshold yet."

Meant to run CONTINUOUSLY during market hours (see run_swing_guardian),
not once a day -- "constantly track the stocks and book or trail profit,"
per explicit instruction. Uses kite.quote() for live price, polled at
POLL_INTERVAL_SECONDS; swing positions move on the order of minutes to
days, so this does not need run_live.py's 1-second intraday cadence.

Trailing-arm threshold is BREAKEVEN-ADJUSTED, not a flat 2% above
avg_price, per explicit instruction ("I need 2% from the breakeven -- if
breakeven is 0.10%, start trailing when the return is 2.10%"). Breakeven
(src.swing_strategy.compute_mtf_breakeven_pct) grows with how long the
position has been held, since the dominant cost is daily MTF funding
interest on the funded (broker-financed) amount -- a position held 2
months needs roughly 2.1% just to cover its own costs, not 0%, so arming
at a flat 2% would mean trailing starts barely above TRUE breakeven by
then, not 2% of real profit above it. The funded amount is fetched PER
POSITION via Kite's own order-margin calculator (_funded_amount_for_position)
rather than assumed, since MTF margin % varies per stock.
"""

import json
import time as time_module
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from . import auth, config, orders
from .swing_strategy import PROFIT_TARGET_PCT, TRAILING_PCT, compute_mtf_breakeven_pct

IST = ZoneInfo("Asia/Kolkata")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SWING_STATE_FILE = PROJECT_ROOT / "swing_positions_state.json"
LOG_DIR = PROJECT_ROOT / "logs"

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
# Swing positions move on the order of minutes to days, not seconds -- no
# need for run_live.py's 1-second intraday cadence (which exists only
# because that endpoint is rate-limited to 1 req/sec and the loop fires
# exactly one call per poll). A moderate interval keeps well under any
# rate limit while still noticing an intraday trailing-stop breach promptly.
POLL_INTERVAL_SECONDS = 30


def _now() -> datetime:
    return datetime.now(IST)


class SwingLogger:
    """Same jsonl-plus-optional-notifier shape as live.py's LiveLogger."""

    NOTIFY_KINDS = {
        "swing_position_detected",
        "swing_trailing_armed",
        "swing_exit",
        "swing_position_closed_externally",
        "swing_funded_amount_unavailable",
        "start",
        "end",
    }

    def __init__(self, log_path: Path, notifier=None):
        self.log_path = log_path
        self.notifier = notifier
        log_path.parent.mkdir(exist_ok=True)

    def _notify(self, text: str) -> None:
        if self.notifier is None:
            return
        try:
            self.notifier(text)
        except Exception as exc:  # noqa: BLE001 - a chat outage must never stop tracking
            print(f"WARNING: notifier failed: {exc}")

    def event(self, kind: str, **fields) -> None:
        record = {"timestamp": _now().isoformat(), "kind": kind, **fields}
        print(f"[{record['timestamp']}] {kind}: {fields}")
        with open(self.log_path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
        if kind in self.NOTIFY_KINDS:
            self._notify(f"{kind}: {fields}")

    def critical(self, message: str, **fields) -> None:
        banner = "!" * 70
        print(f"\n{banner}\nCRITICAL -- MANUAL ACTION NEEDED: {message}\n{banner}\n")
        self.event("CRITICAL", message=message, **fields)
        self._notify(f"⚠️ SWING CRITICAL -- MANUAL ACTION NEEDED\n\n{message}")


def load_state() -> dict[str, dict]:
    """{symbol: {"trailing": bool, "peak_price": float|None, "entry_date":
    "YYYY-MM-DD"|None, "funded_amount": float|None, "last_qty": int|None}}.
    entry_date is a PROXY for the real buy date -- the date this guardian
    first saw the position, not necessarily when it was actually bought (if
    the guardian wasn't running yet at entry, e.g. after a restart gap, this
    under-counts days held and so under-counts breakeven -- see
    _funded_amount_for_position's own caveat on this same limitation).
    funded_amount is this position's real MTF-financed amount from Kite's
    own margin calculator, None if it couldn't be determined (callers must
    then fall back to the flat PROFIT_TARGET_PCT, not assume a leverage).
    A missing file means nothing is tracked yet anywhere -- not an error."""
    if not SWING_STATE_FILE.exists():
        return {}
    with open(SWING_STATE_FILE) as f:
        return json.load(f)


def save_state(state: dict[str, dict]) -> None:
    with open(SWING_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _fetch_mtf_positions(kite) -> dict[str, dict] | None:
    """Current real MTF positions keyed by symbol, straight from Kite --
    the only source of truth for what's actually held and at what average
    price. Returns None (NOT {}) on a fetch failure -- e.g. a stale/expired
    Kite access token, which happens every day Kite isn't freshly logged
    into, or a transient network hiccup. This distinction matters a lot:
    check_once() treats a symbol missing from this result as "closed
    externally" and drops it from tracking. {} on a genuine API failure
    would look EXACTLY like "you hold nothing," which would silently wipe
    every tracked position out of state the moment auth went stale --
    found while reasoning through what happens if this runs across a token
    expiry without a fresh login. None lets the caller skip the whole poll
    instead, leaving existing tracked state untouched until the next
    successful fetch."""
    try:
        net = kite.positions().get("net", [])
    except orders.PLACEMENT_EXCEPTIONS as exc:
        print(f"WARNING: could not fetch positions from Kite: {exc}")
        return None
    return {p["tradingsymbol"]: p for p in net if p.get("product") == orders.PRODUCT_MTF and p.get("quantity", 0) > 0}


def _fetch_ltp(kite, symbols: list[str]) -> dict[str, float]:
    if not symbols:
        return {}
    try:
        quotes = kite.quote([f"NSE:{s}" for s in symbols])
    except orders.PLACEMENT_EXCEPTIONS as exc:
        print(f"WARNING: could not fetch quotes from Kite: {exc}")
        return {}
    result = {}
    for symbol in symbols:
        q = quotes.get(f"NSE:{symbol}")
        if q and q.get("last_price") is not None:
            result[symbol] = float(q["last_price"])
    return result


def _funded_amount_for_position(kite, symbol: str, quantity: int, avg_price: float) -> float | None:
    """Best-effort fetch of THIS position's real MTF funded (broker-financed)
    amount, via Kite's own order-margin calculator (kite.order_margins) --
    not an assumed leverage ratio, since MTF margin % is set per stock.

    Computed as position_value - required_margin, where required_margin
    comes from Kite's response to a hypothetical fresh BUY of this exact
    quantity/symbol/product -- the actual margin already locked in in
    Kite's books for this real position may differ slightly if Kite's
    margin table for this stock changed since the real entry, but this is
    the closest available estimate without a documented endpoint for
    reading an existing position's original margin breakdown.

    Returns None (never raises) if the call fails or the response doesn't
    have the expected shape -- callers must fall back to the flat
    PROFIT_TARGET_PCT in that case rather than silently guessing."""
    try:
        result = kite.order_margins(
            [
                {
                    "exchange": "NSE",
                    "tradingsymbol": symbol,
                    "transaction_type": "BUY",
                    "variety": "regular",
                    "product": orders.PRODUCT_MTF,
                    "order_type": "MARKET",
                    "quantity": quantity,
                    "price": 0,
                    "trigger_price": 0,
                }
            ]
        )
        required_margin = float(result[0]["total"])
    except orders.PLACEMENT_EXCEPTIONS + (KeyError, IndexError, TypeError, ValueError) as exc:
        print(f"WARNING: could not compute MTF funded amount for {symbol} via order_margins: {exc}")
        return None
    position_value = quantity * avg_price
    return max(0.0, position_value - required_margin)


def _arm_threshold_pct(kite, symbol: str, pos: dict, st: dict, today: date, logger: SwingLogger) -> float:
    """breakeven% (for however long this position has actually been held) +
    PROFIT_TARGET_PCT -- see this module's own docstring for why the arm
    point moves with holding period instead of staying a flat 2%. Falls
    back to the flat PROFIT_TARGET_PCT alone if the funded amount isn't
    known (logged once, not every poll, via the caller checking
    st['funded_amount'] is already None before calling this with a fresh
    fetch attempt)."""
    if st["funded_amount"] is None:
        return PROFIT_TARGET_PCT
    entry_date = date.fromisoformat(st["entry_date"])
    days_held = max(0, (today - entry_date).days)
    position_value = pos["quantity"] * pos["average_price"]
    breakeven_pct = compute_mtf_breakeven_pct(position_value, st["funded_amount"], days_held)
    return breakeven_pct + PROFIT_TARGET_PCT


def check_once(kite, state: dict[str, dict], logger: SwingLogger) -> None:
    """One pass: reconcile tracked state against Kite's real MTF positions,
    then apply breakeven-adjusted-profit-target-arm / trailing-stop logic
    and place any real exit that triggers. Mutates `state` in place; caller
    is responsible for persisting it (see run_swing_guardian)."""
    today = _now().date()
    positions = _fetch_mtf_positions(kite)
    if positions is None:
        # Fetch failed -- most likely a stale/expired Kite access token (these
        # expire every day; see run_swing_guardian's own docstring on needing a
        # fresh login daily) or a transient network issue. Skip this poll
        # entirely rather than risk treating "couldn't check" as "nothing is
        # held anymore" -- existing tracked state (and any already-armed
        # trailing stop) is left exactly as it was for the next successful poll.
        logger.event("swing_poll_skipped_fetch_failed")
        return

    for symbol in list(state.keys()):
        if symbol not in positions:
            logger.event("swing_position_closed_externally", symbol=symbol)
            del state[symbol]

    for symbol, pos in positions.items():
        if symbol not in state:
            funded_amount = _funded_amount_for_position(kite, symbol, pos["quantity"], pos["average_price"])
            if funded_amount is None:
                logger.event("swing_funded_amount_unavailable", symbol=symbol)
            state[symbol] = {
                "trailing": False,
                "peak_price": None,
                "entry_date": today.isoformat(),
                "funded_amount": funded_amount,
                "last_qty": pos["quantity"],
            }
            logger.event(
                "swing_position_detected",
                symbol=symbol,
                qty=pos["quantity"],
                avg_price=pos["average_price"],
                funded_amount=funded_amount,
            )
        elif pos["quantity"] != state[symbol].get("last_qty"):
            # Quantity changed since last poll -- a manual averaging leg or partial
            # sell -- re-fetch the funded amount against the new quantity/avg_price
            # rather than let it go stale. Keeps the ORIGINAL entry_date (see
            # load_state's own caveat on why this can understate days_held for an
            # averaged-in leg, which makes breakeven a slight UNDERestimate for
            # that leg specifically but an overestimate for the position as a
            # whole once blended -- net effect is a conservative, not reckless,
            # bias, which is the safer direction to be wrong in).
            funded_amount = _funded_amount_for_position(kite, symbol, pos["quantity"], pos["average_price"])
            state[symbol]["funded_amount"] = funded_amount
            state[symbol]["last_qty"] = pos["quantity"]
            if funded_amount is None:
                logger.event("swing_funded_amount_unavailable", symbol=symbol)

    if not positions:
        return

    ltps = _fetch_ltp(kite, list(positions.keys()))

    for symbol, pos in positions.items():
        ltp = ltps.get(symbol)
        if ltp is None:
            continue
        avg_price = pos["average_price"]
        st = state[symbol]

        if st["trailing"]:
            st["peak_price"] = max(st["peak_price"], ltp)
            stop_level = st["peak_price"] * (1 - TRAILING_PCT)
            if ltp > stop_level:
                continue

            qty = positions[symbol]["quantity"]  # re-read fresh, never cached, in case of a manual partial sell
            try:
                order_id = orders.place_mtf_order(kite, symbol, "SELL", qty, ltp, tag="swing_exit")
            except orders.OrderRejected as exc:
                logger.critical(
                    f"{symbol} swing EXIT order was REJECTED by Kite -- the real position is almost certainly "
                    f"still open: {exc}",
                    symbol=symbol,
                )
                continue
            except orders.OrderPlacementAmbiguous as exc:
                logger.critical(f"{symbol} swing EXIT order placement is in an UNKNOWN state -- {exc}", symbol=symbol)
                continue

            fill = orders.wait_for_fill(kite, order_id)
            if fill["status"] != "COMPLETE":
                logger.critical(
                    f"{symbol} swing EXIT did not confirm filled -- real position may still be open. "
                    "Check Kite directly.",
                    symbol=symbol,
                    fill_result=fill,
                )
                continue

            fill_price = fill["average_price"] or ltp
            filled_qty = fill["filled_quantity"] or qty
            gross_pnl = (fill_price - avg_price) * filled_qty
            logger.event(
                "swing_exit",
                symbol=symbol,
                qty=filled_qty,
                price=fill_price,
                avg_price=avg_price,
                gross_pnl=round(gross_pnl, 2),
                note="gross only -- excludes MTF daily funding interest and entry-side charges, which this "
                "module never saw (entry was manual); see Kite's own ledger for the true net figure",
            )
            del state[symbol]
        else:
            arm_pct = _arm_threshold_pct(kite, symbol, pos, st, today, logger)
            if ltp >= avg_price * (1 + arm_pct):
                st["trailing"] = True
                st["peak_price"] = ltp
                logger.event(
                    "swing_trailing_armed", symbol=symbol, avg_price=avg_price, armed_at=ltp, arm_pct=round(arm_pct * 100, 3)
                )


def run_swing_guardian(poll_interval: int = POLL_INTERVAL_SECONDS, notifier=None, should_stop=None) -> None:
    """Continuously track real MTF positions from market open to close,
    auto-exiting on a trailing-stop breach. No CONFIRM gate -- see this
    module's own docstring for why that's safe here specifically (exit-only,
    never a new entry).

    should_stop() -> bool, polled each cycle, same panic-button contract as
    run_live.py's should_stop: ends the loop, does not place any order on
    its own (an in-progress trailing stop already breached still fires --
    this is a monitoring stop, not a square-off).
    """
    if not config.LIVE_TRADING_ENABLED:
        raise RuntimeError(
            "LIVE_TRADING_ENABLED is not set to 'true' in .env. This is a deliberate safety "
            "gate -- add it explicitly if you actually intend to place real exit orders."
        )

    kite = auth.get_kite()
    today = _now().date()
    logger = SwingLogger(LOG_DIR / f"swing_{today.isoformat()}.jsonl", notifier=notifier)
    state = load_state()

    logger.event("start", tracked=list(state.keys()))

    try:
        while True:
            if should_stop is not None and should_stop():
                logger.event("stop_requested")
                break

            now = _now().time()
            if now < MARKET_OPEN or now >= MARKET_CLOSE:
                time_module.sleep(min(poll_interval, 60))
                continue

            try:
                check_once(kite, state, logger)
            except Exception as exc:  # noqa: BLE001 - see intraday_live.py's identical
                # guard and comment for the real incident (an order-tag-length bug)
                # that crashed that guardian entirely and abandoned every tracked
                # position for the rest of the day. One bad poll must never take
                # down protection on everything else being watched.
                logger.critical(f"check_once() raised an unexpected exception -- continuing to poll, but investigate now: {exc!r}")
            save_state(state)
            time_module.sleep(poll_interval)
    finally:
        logger.event("end", tracked=list(state.keys()))
