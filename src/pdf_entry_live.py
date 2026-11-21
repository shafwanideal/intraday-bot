"""Part 2 of the trading system: a fully separate, isolated add-on that turns
a Kush-Bohra-style pick (buy range, stop-loss, target 1, target 2) into real
orders -- entry via a GTT/limit buy, then a 50/50 split exit: half booked
HARD at T1 via a broker-side GTT (so it's locked in even if this process is
down), the other half's stop raised to a floor at T1 and trailed (same
floor-lock idea as swing_live.py's own TRAILING_PCT) toward T2.

Explicit requirement (2026-11-21, from the trader): this must be purely
additive and must NEVER modify swing_live.py, intraday_live.py,
hourly_pnl.py, digest.py or alerts.py -- the existing exit-only guardian
(part 1, "the sell bot") is working correctly and must not be touched or put
at risk. This module:
  - has its own state file (pdf_entry_state.json) and log file
    (pdf_entry_<date>.jsonl), never touching swing_positions_state.json or
    either guardian's own jsonl logs.
  - only imports PUBLIC, pre-existing, read-only-in-effect helpers
    (orders.place_mtf_order/place_delivery_order/wait_for_fill,
    orders.PRODUCT_MTF, orders.PLACEMENT_EXCEPTIONS) that already existed
    before this file did -- it adds no new parameter to any of them.
  - is run as its own separate process (see scripts/run_pdf_entry_bot.py),
    never merged into scripts/run_all_guardians.py's thread list.
  - once a position is live, the existing exit-only guardian will see its
    MTF/CNC holding show up in kite.positions() like any other -- but this
    module always has its own GTT covering every open share (see
    _place_exit_gtts), so swing_live.py's own crash-stop-loss logic firing a
    SELL on the same symbol is a redundant safety net, not a conflict: Kite
    simply fills whichever leg triggers first, and the other side's GTT is
    then cancelled (see _reconcile_gtt_vs_manual_sell).

GTT response-shape note: place_gtt()'s create response is assumed to return
{"trigger_id": <int>}, and get_gtts()/get_gtt() list entries are assumed to
key the trigger's own id as "id" (both per Kite's public API docs). This
has NOT been verified against a real account from this coding session (no
live Kite credentials here) -- the very first real GTT placed on the VPS
should be manually cross-checked in the Kite app before trusting this
module unattended, exactly like every other live-API assumption in this
project has been checked for real at least once.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import auth, heartbeat, orders
from .orders import PLACEMENT_EXCEPTIONS, PRODUCT_MTF

# A SEPARATE gate from config.LIVE_TRADING_ENABLED (which only ever means
# "the existing exit-only guardians may place exit orders"). This module
# places real ENTRY orders with real capital -- overloading the existing
# flag's meaning would silently turn this on for anyone who only ever meant
# to enable the exit-only guardians, so it gets its own explicit opt-in,
# read directly from the environment rather than via config.py (keeps this
# file from adding even one new line to config.py).
PDF_ENTRY_TRADING_ENABLED = os.environ.get("PDF_ENTRY_TRADING_ENABLED", "").strip().lower() == "true"

IST = ZoneInfo("Asia/Kolkata")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"

# A plain JSON list the trader (or a future PDF-parsing step) drops new picks
# into -- consumed and cleared every poll. Deliberately NOT a Telegram
# command, to avoid touching telegram_control.py for this first version.
PICKS_FILE = LOG_DIR / "pending_picks.json"
STATE_FILE = LOG_DIR / "pdf_entry_state.json"

POLL_INTERVAL_SECONDS = 30

# Same tightened trail value the exit-only guardian settled on after its own
# backtest (swing_live.py's TRAILING_PCT) -- reused here as a plain constant,
# not imported, so this file has zero import-time dependency on swing_live.py.
TRAIL_PCT = 0.005

# Matches the KB-technical skill's own "don't chase" rule: a fresh buy more
# than this far above the top of the quoted range is not the trade that was
# actually recommended any more.
CHASE_TOLERANCE_PCT = 0.015

# A GTT's LIMIT leg is placed this much through its trigger so it still fills
# even if price gaps slightly past the trigger before the resting order posts.
GTT_FILL_BUFFER_PCT = 0.003

PHASE_AWAITING_ENTRY = "awaiting_entry"
PHASE_ENTRY_GTT_PENDING = "entry_gtt_pending"
PHASE_AWAITING_EXIT_SETUP = "awaiting_exit_setup"
PHASE_MONITORING = "monitoring"
PHASE_CLOSED = "closed"
PHASE_SKIPPED_CHASE = "skipped_chase"
PHASE_ERROR = "error"

TERMINAL_PHASES = {PHASE_CLOSED, PHASE_SKIPPED_CHASE, PHASE_ERROR}


def _now() -> datetime:
    return datetime.now(IST)


class PdfEntryLogger:
    """Self-contained jsonl-plus-notifier logger -- same shape as
    swing_live.py's SwingLogger, but its own class, with no import of it."""

    NOTIFY_KINDS = {
        "pdf_entry_picked_up",
        "pdf_entry_filled",
        "pdf_entry_skipped_chase",
        "pdf_entry_exit_gtts_placed",
        "pdf_entry_t1_hit",
        "pdf_entry_t2_hit",
        "pdf_entry_sl_hit",
        "pdf_entry_error",
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
            self._notify(_format_event(kind, fields))


def _format_event(kind: str, fields: dict) -> str:
    sym = fields.get("symbol", "?")
    if kind == "pdf_entry_picked_up":
        return (f"📥 New pick loaded: {sym} — buy {fields['buy_low']}-{fields['buy_high']}, "
                f"SL {fields['sl']}, T1 {fields['t1']}, T2 {fields['t2']}")
    if kind == "pdf_entry_filled":
        return f"✅ {sym}: entry filled — qty {fields['qty']} @ ₹{fields['avg_price']:.2f}"
    if kind == "pdf_entry_skipped_chase":
        return f"⏭️ {sym}: skipped — price (₹{fields['ltp']:.2f}) ran above the buy range before entry, not chasing"
    if kind == "pdf_entry_exit_gtts_placed":
        return (f"🎯 {sym}: exit GTTs live — {fields['half1_qty']} shares to T1 ₹{fields['t1']}, "
                f"{fields['half2_qty']} shares to T2 ₹{fields['t2']}, both stopped at ₹{fields['sl']}")
    if kind == "pdf_entry_t1_hit":
        return (f"🏆 {sym}: T1 hit — booked {fields['half1_qty']} shares near ₹{fields['t1']:.2f}. "
                f"Remaining {fields['half2_qty']} now floored at ₹{fields['new_floor']:.2f}, trailing toward T2 ₹{fields['t2']}")
    if kind == "pdf_entry_t2_hit":
        return f"🏁 {sym}: T2 reached — remaining {fields['half2_qty']} shares booked near ₹{fields['t2']:.2f}. Position fully closed."
    if kind == "pdf_entry_sl_hit":
        return f"🛑 {sym}: stop-loss hit on {fields['qty']} shares near ₹{fields['price']:.2f}"
    if kind == "pdf_entry_error":
        return f"⚠️ {sym}: {fields.get('message', 'error')}"
    return f"{kind}: {fields}"


def load_state() -> dict[str, dict]:
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict[str, dict]) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    tmp.replace(STATE_FILE)


def _consume_pending_picks(state: dict[str, dict], logger: PdfEntryLogger) -> None:
    """Reads PICKS_FILE (a plain JSON list of new picks), adds any symbol not
    already in flight into state as 'awaiting_entry', then clears the file --
    so re-running the loop never double-adds the same pick. A symbol already
    in flight (any non-terminal phase) is left alone and flagged, not
    silently overwritten."""
    if not PICKS_FILE.exists():
        return
    try:
        with open(PICKS_FILE) as f:
            picks = json.load(f)
    except (json.JSONDecodeError, OSError):
        return
    if not picks:
        PICKS_FILE.unlink(missing_ok=True)
        return
    for pick in picks:
        symbol = pick["symbol"]
        existing = state.get(symbol)
        if existing and existing["phase"] not in TERMINAL_PHASES:
            logger.event("pdf_entry_error", symbol=symbol,
                         message="a pick for this symbol is already in flight -- ignoring the new one")
            continue
        state[symbol] = {
            "phase": PHASE_AWAITING_ENTRY,
            "buy_low": float(pick["buy_low"]),
            "buy_high": float(pick["buy_high"]),
            "sl": float(pick["sl"]),
            "t1": float(pick["t1"]),
            "t2": float(pick["t2"]),
            "product": pick.get("product", PRODUCT_MTF),
            "capital": pick.get("capital"),
            "quantity": pick.get("quantity"),
            "entry_gtt_id": None,
            "qty": None,
            "avg_price": None,
            "half1_qty": None,
            "half2_qty": None,
            "gtt1_id": None,
            "gtt2_id": None,
            "gtt2_floor": None,
            "half2_peak": None,
            "created": _now().isoformat(),
        }
        logger.event("pdf_entry_picked_up", symbol=symbol, buy_low=pick["buy_low"], buy_high=pick["buy_high"],
                     sl=pick["sl"], t1=pick["t1"], t2=pick["t2"])
    PICKS_FILE.unlink(missing_ok=True)


def _quantity_for(st: dict) -> int:
    if st.get("quantity"):
        return int(st["quantity"])
    capital = st.get("capital") or 0.0
    return int(capital // st["buy_high"])


def _get_ltp(kite, symbol: str) -> float | None:
    try:
        q = kite.quote(f"NSE:{symbol}")
        return q[f"NSE:{symbol}"]["last_price"]
    except PLACEMENT_EXCEPTIONS:
        return None


def _place_entry_order(kite, symbol: str, product: str, qty: int, reference_price: float):
    if product == PRODUCT_MTF:
        return orders.place_mtf_order(kite, symbol, "BUY", qty, reference_price, tag="pdf_entry")
    return orders.place_delivery_order(kite, symbol, "BUY", qty, reference_price, tag="pdf_entry")


def _advance_awaiting_entry(kite, symbol: str, st: dict, logger: PdfEntryLogger) -> None:
    """Buys now (market) if the LTP is already inside the range (plus a
    little chase room); arms a single-leg GTT at buy_low if price hasn't
    reached the range yet, so it only fires once price actually enters it
    from below; skips entirely (no trade) if price has already run too far
    above the range -- same 'don't chase' principle the KB-technical skill
    itself uses for next-day picks."""
    ltp = _get_ltp(kite, symbol)
    if ltp is None:
        return  # transient quote failure -- just try again next poll
    buy_low, buy_high = st["buy_low"], st["buy_high"]
    chase_ceiling = buy_high * (1 + CHASE_TOLERANCE_PCT)

    if ltp > chase_ceiling and not st.get("entry_gtt_id"):
        st["phase"] = PHASE_SKIPPED_CHASE
        logger.event("pdf_entry_skipped_chase", symbol=symbol, ltp=ltp, buy_high=buy_high)
        return

    if buy_low <= ltp <= chase_ceiling:
        qty = _quantity_for(st)
        if qty <= 0:
            st["phase"] = PHASE_ERROR
            logger.event("pdf_entry_error", symbol=symbol, message=f"computed quantity is {qty} -- check capital/buy_high")
            return
        if st.get("entry_gtt_id"):
            try:
                kite.delete_gtt(st["entry_gtt_id"])
            except PLACEMENT_EXCEPTIONS:
                pass
        try:
            order_id = _place_entry_order(kite, symbol, st["product"], qty, ltp)
            result = orders.wait_for_fill(kite, order_id)
        except orders.OrderRejected as exc:
            st["phase"] = PHASE_ERROR
            logger.event("pdf_entry_error", symbol=symbol, message=f"entry order rejected: {exc}")
            return
        except orders.OrderPlacementAmbiguous as exc:
            logger.event("pdf_entry_error", symbol=symbol, message=f"entry order placement ambiguous, needs manual check: {exc}")
            return  # don't advance phase -- a human must check Kite before this retries
        if result["status"] != "COMPLETE" or not result["filled_quantity"]:
            logger.event("pdf_entry_error", symbol=symbol, message=f"entry order did not complete: {result['status']}")
            return
        st["qty"] = result["filled_quantity"]
        st["avg_price"] = result["average_price"]
        st["phase"] = PHASE_AWAITING_EXIT_SETUP
        logger.event("pdf_entry_filled", symbol=symbol, qty=st["qty"], avg_price=st["avg_price"])
        return

    # ltp < buy_low: wait for price to rise into the range
    if st.get("entry_gtt_id"):
        return  # already armed -- just keep waiting
    qty = _quantity_for(st)
    if qty <= 0:
        st["phase"] = PHASE_ERROR
        logger.event("pdf_entry_error", symbol=symbol, message=f"computed quantity is {qty} -- check capital/buy_high")
        return
    try:
        resp = kite.place_gtt(
            trigger_type=kite.GTT_TYPE_SINGLE,
            tradingsymbol=symbol,
            exchange=kite.EXCHANGE_NSE,
            trigger_values=[buy_low],
            last_price=ltp,
            orders=[{
                "transaction_type": kite.TRANSACTION_TYPE_BUY,
                "quantity": qty,
                "order_type": kite.ORDER_TYPE_LIMIT,
                "product": st["product"],
                "price": buy_high,
            }],
        )
        st["entry_gtt_id"] = resp["trigger_id"]
        st["quantity"] = qty
        st["phase"] = PHASE_ENTRY_GTT_PENDING
    except PLACEMENT_EXCEPTIONS as exc:
        logger.event("pdf_entry_error", symbol=symbol, message=f"could not place entry GTT: {exc}")


def _find_recent_buy_fill(kite, symbol: str, product: str, qty: int):
    """Best-effort match for the order a just-triggered entry GTT produced --
    same idea as orders.py's own _find_recent_matching_order, kept local here
    so this file imports nothing private from orders.py."""
    try:
        all_orders = kite.orders()
    except PLACEMENT_EXCEPTIONS:
        return None
    for o in reversed(all_orders):
        if (o.get("tradingsymbol") == symbol and o.get("transaction_type") == "BUY"
                and o.get("product") == product and o.get("status") == "COMPLETE"):
            return o
    return None


def _advance_entry_gtt_pending(kite, symbol: str, st: dict, logger: PdfEntryLogger) -> None:
    try:
        gtt = kite.get_gtt(st["entry_gtt_id"])
    except PLACEMENT_EXCEPTIONS:
        return
    if gtt.get("status") != kite.GTT_STATUS_TRIGGERED:
        return
    fill = _find_recent_buy_fill(kite, symbol, st["product"], st["quantity"])
    if fill is None:
        return  # the GTT fired but the resulting order hasn't shown up yet -- check again next poll
    st["qty"] = fill.get("filled_quantity") or st["quantity"]
    st["avg_price"] = fill.get("average_price")
    st["phase"] = PHASE_AWAITING_EXIT_SETUP
    logger.event("pdf_entry_filled", symbol=symbol, qty=st["qty"], avg_price=st["avg_price"])


def _place_oco_gtt(kite, symbol: str, product: str, qty: int, sl: float, target: float, ltp: float):
    sl_price = round(sl * (1 - GTT_FILL_BUFFER_PCT), 2)
    target_price = round(target * (1 - GTT_FILL_BUFFER_PCT), 2)
    resp = kite.place_gtt(
        trigger_type=kite.GTT_TYPE_OCO,
        tradingsymbol=symbol,
        exchange=kite.EXCHANGE_NSE,
        trigger_values=[sl, target],
        last_price=ltp,
        orders=[
            {"transaction_type": "SELL", "quantity": qty, "order_type": kite.ORDER_TYPE_LIMIT, "product": product, "price": sl_price},
            {"transaction_type": "SELL", "quantity": qty, "order_type": kite.ORDER_TYPE_LIMIT, "product": product, "price": target_price},
        ],
    )
    return resp["trigger_id"]


def _advance_awaiting_exit_setup(kite, symbol: str, st: dict, logger: PdfEntryLogger) -> None:
    qty = st["qty"]
    ltp = _get_ltp(kite, symbol) or st["avg_price"]
    if qty == 1:
        # Can't split a single share -- run the whole thing as one T2 bracket.
        half1, half2 = 0, 1
    else:
        half1 = qty // 2
        half2 = qty - half1
    try:
        gtt2_id = _place_oco_gtt(kite, symbol, st["product"], half2, st["sl"], st["t2"], ltp)
        gtt1_id = _place_oco_gtt(kite, symbol, st["product"], half1, st["sl"], st["t1"], ltp) if half1 else None
    except PLACEMENT_EXCEPTIONS as exc:
        logger.event("pdf_entry_error", symbol=symbol, message=f"could not place exit GTTs: {exc}")
        return
    st["half1_qty"] = half1
    st["half2_qty"] = half2
    st["gtt1_id"] = gtt1_id
    st["gtt2_id"] = gtt2_id
    st["gtt2_floor"] = st["sl"]
    st["half2_peak"] = st["avg_price"]
    st["phase"] = PHASE_MONITORING
    logger.event("pdf_entry_exit_gtts_placed", symbol=symbol, half1_qty=half1, half2_qty=half2, sl=st["sl"], t1=st["t1"], t2=st["t2"])


def _gtt_status(kite, trigger_id):
    try:
        return kite.get_gtt(trigger_id)
    except PLACEMENT_EXCEPTIONS:
        return None


def _advance_monitoring(kite, symbol: str, st: dict, logger: PdfEntryLogger) -> None:
    # --- half1 (T1 bracket), only if it exists ---
    if st["gtt1_id"] and st["half1_qty"]:
        gtt1 = _gtt_status(kite, st["gtt1_id"])
        if gtt1 is not None and gtt1.get("status") == kite.GTT_STATUS_TRIGGERED:
            fill = _find_recent_sell_fill(kite, symbol, st["product"], st["half1_qty"])
            price = fill.get("average_price") if fill else st["t1"]
            hit_target = price is not None and abs(price - st["t1"]) <= abs(price - st["sl"])
            if hit_target:
                new_floor = max(st["gtt2_floor"], st["t1"])
                _raise_gtt2_floor(kite, symbol, st, new_floor, logger, reason="t1_hit")
                logger.event("pdf_entry_t1_hit", symbol=symbol, half1_qty=st["half1_qty"],
                             t1=st["t1"], new_floor=new_floor, half2_qty=st["half2_qty"], t2=st["t2"])
            else:
                logger.event("pdf_entry_sl_hit", symbol=symbol, qty=st["half1_qty"], price=price or st["sl"])
            st["gtt1_id"] = None  # this leg is resolved either way

    if not (st["gtt2_id"] and st["half2_qty"]):
        if not st["gtt1_id"]:
            st["phase"] = PHASE_CLOSED
        return

    # --- half2 (T2 bracket, with a trailing floor once T1 has fired) ---
    gtt2 = _gtt_status(kite, st["gtt2_id"])
    if gtt2 is not None and gtt2.get("status") == kite.GTT_STATUS_TRIGGERED:
        fill = _find_recent_sell_fill(kite, symbol, st["product"], st["half2_qty"])
        price = fill.get("average_price") if fill else st["t2"]
        hit_target = price is not None and abs(price - st["t2"]) <= abs(price - st["gtt2_floor"])
        if hit_target:
            logger.event("pdf_entry_t2_hit", symbol=symbol, half2_qty=st["half2_qty"], t2=st["t2"], price=price)
        else:
            logger.event("pdf_entry_sl_hit", symbol=symbol, qty=st["half2_qty"], price=price)
        st["gtt2_id"] = None
        if not st["gtt1_id"]:
            st["phase"] = PHASE_CLOSED
        return

    # Only trail once T1 has already raised the floor off the original SL --
    # before that, half2 just sits on its original GTT, untouched.
    if st["gtt2_floor"] <= st["sl"]:
        return
    ltp = _get_ltp(kite, symbol)
    if ltp is None:
        return
    st["half2_peak"] = max(st["half2_peak"] or ltp, ltp)
    trail_floor = st["half2_peak"] * (1 - TRAIL_PCT)
    new_floor = max(st["gtt2_floor"], trail_floor)
    if new_floor > st["gtt2_floor"]:
        _raise_gtt2_floor(kite, symbol, st, new_floor, logger, reason="trailing")


def _find_recent_sell_fill(kite, symbol: str, product: str, qty: int):
    try:
        all_orders = kite.orders()
    except PLACEMENT_EXCEPTIONS:
        return None
    for o in reversed(all_orders):
        if (o.get("tradingsymbol") == symbol and o.get("transaction_type") == "SELL"
                and o.get("product") == product and o.get("status") == "COMPLETE"
                and o.get("filled_quantity") == qty):
            return o
    return None


def _raise_gtt2_floor(kite, symbol: str, st: dict, new_floor: float, logger: PdfEntryLogger, reason: str) -> None:
    """Cancels the current half2 GTT and re-places it with a higher SL leg
    (the floor), same target T2 -- the floor can only ever rise, mirroring
    swing_live.py's own floor-lock guarantee, just reimplemented here as a
    GTT modify instead of an in-process stop level."""
    ltp = _get_ltp(kite, symbol) or new_floor
    try:
        kite.delete_gtt(st["gtt2_id"])
        st["gtt2_id"] = _place_oco_gtt(kite, symbol, st["product"], st["half2_qty"], new_floor, st["t2"], ltp)
        st["gtt2_floor"] = new_floor
    except PLACEMENT_EXCEPTIONS as exc:
        logger.event("pdf_entry_error", symbol=symbol, message=f"could not raise floor to {new_floor} ({reason}): {exc}")


def check_once(kite, state: dict[str, dict], logger: PdfEntryLogger) -> None:
    _consume_pending_picks(state, logger)
    for symbol, st in list(state.items()):
        phase = st["phase"]
        if phase == PHASE_AWAITING_ENTRY:
            _advance_awaiting_entry(kite, symbol, st, logger)
        elif phase == PHASE_ENTRY_GTT_PENDING:
            _advance_entry_gtt_pending(kite, symbol, st, logger)
        elif phase == PHASE_AWAITING_EXIT_SETUP:
            _advance_awaiting_exit_setup(kite, symbol, st, logger)
        elif phase == PHASE_MONITORING:
            _advance_monitoring(kite, symbol, st, logger)
    save_state(state)


def run_pdf_entry_bot(poll_interval: int = POLL_INTERVAL_SECONDS, notifier=None, should_stop=None) -> None:
    """Runs forever as its own process (see scripts/run_pdf_entry_bot.py) --
    NOT a thread inside run_all_guardians.py, so a bug here can never take
    down the existing swing/intraday guardians, and nothing here is ever
    imported by them either.

    Same get-kite-once-then-refresh-each-tick pattern as
    swing_live.run_swing_guardian (auth.get_kite / auth.refresh_client_if_changed
    are both pre-existing, read-only-in-effect helpers -- calling them here
    adds nothing to auth.py itself)."""
    import time as time_module

    if not PDF_ENTRY_TRADING_ENABLED:
        raise RuntimeError(
            "PDF_ENTRY_TRADING_ENABLED is not set to 'true' in .env. This is a deliberate "
            "separate safety gate for the PDF/pick entry-automation bot -- add it explicitly "
            "if you actually intend this to place real entry and exit orders."
        )

    kite = auth.get_kite()
    today = _now().date()
    logger = PdfEntryLogger(LOG_DIR / f"pdf_entry_{today.isoformat()}.jsonl", notifier=notifier)
    state = load_state()
    logger.event("start", tracked=list(state.keys()))

    while True:
        if should_stop is not None and should_stop():
            logger.event("end", tracked=list(state.keys()))
            return

        heartbeat.touch("pdf_entry")

        current_date = _now().date()
        if current_date != today:
            today = current_date
            logger.log_path = LOG_DIR / f"pdf_entry_{today.isoformat()}.jsonl"

        fresh = auth.refresh_client_if_changed(kite)
        if fresh is not kite:
            kite = fresh
            logger.event("kite_token_refreshed")

        try:
            check_once(kite, state, logger)
        except Exception as exc:  # noqa: BLE001 - one bad tick must never kill the whole process
            logger.event("pdf_entry_error", symbol="ALL", message=f"check_once failed: {exc!r}")
        time_module.sleep(poll_interval)
