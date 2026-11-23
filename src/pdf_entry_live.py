"""Part 2 of the trading system: a fully separate, isolated add-on that
manages the EXIT side of a Kush-Bohra-style pick (stop-loss, target 1,
target 2) once the trader has bought it himself.

Explicit scope (2026-11-21, from the trader): all entries are placed by HIM,
manually, in MTF, after he's seen the PDF -- this module never buys
anything. He gives a symbol + SL + T1 + T2 (by uploading the PDF or just
telling me the levels); once his MTF buy shows up in kite.positions(), this
module splits it 50/50 and places two broker-side OCO GTTs: half booked HARD
at T1, the other half's stop raised to a floor at his ORIGINAL BUY PRICE
(breakeven -- "protect my capital", explicit instruction 2026-11-22, changed
from an earlier version that floored at T1 instead) and trailed further (same
floor-lock idea as swing_live.py's own TRAILING_PCT) toward T2. This is
exit-only, same as the existing guardian -- the only difference is where
SL/T1/T2 come from (an external pick, not a self-computed breakeven/arm
threshold).

Explicit requirement (2026-11-21, from the trader): this must be purely
additive and must NEVER modify swing_live.py, intraday_live.py,
hourly_pnl.py, digest.py or alerts.py -- the existing exit-only guardian
(part 1, "the sell bot") is working correctly and must not be touched or put
at risk. This module:
  - has its own state file (pdf_entry_state.json) and log file
    (pdf_entry_<date>.jsonl), never touching swing_positions_state.json or
    either guardian's own jsonl logs.
  - only imports PUBLIC, pre-existing, read-only-in-effect helpers
    (auth.get_kite/refresh_client_if_changed, heartbeat.touch,
    orders.PLACEMENT_EXCEPTIONS, orders.PRODUCT_MTF) that already existed
    before this file did -- it adds no new parameter to any of them, and
    never calls any of orders.py's order-PLACING functions (place_market_
    order/place_delivery_order/place_mtf_order) at all, since it never buys.
  - is run as its own separate process (see scripts/run_pdf_entry_bot.py),
    never merged into scripts/run_all_guardians.py's thread list.
  - if it finds an existing active GTT on the symbol when the position is
    first detected (e.g. the trader's own manually-placed GTT, or a stale
    one from an earlier run), it does NOT place a second, overlapping
    bracket -- it stops and flags for a human to check, since guessing which
    GTT should win is not this module's call to make.

GTT response-shape note: place_gtt()'s create response is assumed to return
{"trigger_id": <int>}, and get_gtts()/get_gtt() list entries are assumed to
key the trigger's own id as "id" (both per Kite's public API docs). This
has NOT been verified against a real account from this coding session (no
live Kite credentials here) -- the very first real GTT placed on the VPS
should be manually cross-checked in the Kite app before trusting this
module unattended, exactly like every other live-API assumption in this
project has been checked for real at least once.

AUTO-ADOPT sub-flow (added 2026-11-22, explicit instruction): a SECOND,
completely independent way a symbol enters Part 2 -- no PDF, no chat
message, no pending_picks.json entry at all. The trader's own rule: "for
part one I don't place a stop-loss myself -- so when you see a stock with
one already placed, that means it's part two." Concretely: _scan_for_auto_
adopt runs every poll, looking at every MTF holding not already tracked for
a self-placed SINGLE-LEG stop-loss GTT (a plain SL, no target -- a two-leg
GTT is a different thing entirely, see fetch_manual_gtt_info's own meaning
in swing_live.py). The moment one is found:

  1. It's immediately converted into a two-leg (OCO) GTT -- same SL, plus a
     placeholder target far enough out that it should never realistically
     fire (AUTO_PLACEHOLDER_TARGET_MULTIPLE). This is the trick that keeps
     swing_live.py completely untouched: that file's own fetch_manual_gtt_
     info only ever recognises TWO-LEG GTTs as "manually managed, stand
     down" -- converting here means Part 1 stands down from the moment of
     detection, with zero code changes anywhere else.
  2. T1 = breakeven% + 2%, T2 = breakeven% + 5% (both above avg_price,
     recomputed fresh every poll as days_held/funded_amount evolve -- same
     formula shape as swing_live.py's own arm threshold, just a different
     constant and reimplemented locally).
  3. Nothing happens until price reaches T1. At T1, the full quantity is
     split 50/50 into two GTTs: one actively trails 0.5% below peak, the
     other sits pinned exactly at T1.
  4. If the trailing half's stop fires BEFORE T2 is ever reached -- that's
     a 50% partial sale. The other half stays pinned at T1 (a floor, not
     actively trailing) until either it also gets stopped out there, or
     price reaches T2, at which point trailing resumes on just that half.
  5. If instead price reaches T2 WITHOUT the trailing half ever stopping
     out -- no partial sale happens at all. Both halves merge back into one
     full-quantity trailing bracket (floored at T2), and the eventual stop
     sells the ENTIRE position in one exit instead of two half-exits.

Every GTT-raise in this sub-flow is a cancel-and-replace, same pattern as
the pick-based flow's _raise_gtt2_floor -- the floor can only ever rise.
"""

import json
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import auth, heartbeat
from .orders import PLACEMENT_EXCEPTIONS, PRODUCT_MTF
from .swing_strategy import compute_mtf_breakeven_pct

# A SEPARATE gate from config.LIVE_TRADING_ENABLED (which only ever means
# "the existing exit-only guardians may place exit orders"). Even though
# this module never buys, it still places real SELL-side GTTs for real
# money -- overloading the existing flag's meaning would silently turn this
# on for anyone who only ever meant to enable the exit-only guardians, so it
# gets its own explicit opt-in, read directly from the environment rather
# than via config.py (keeps this file from adding even one new line to
# config.py).
PDF_ENTRY_TRADING_ENABLED = os.environ.get("PDF_ENTRY_TRADING_ENABLED", "").strip().lower() == "true"

IST = ZoneInfo("Asia/Kolkata")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"

# A plain JSON list the trader drops new picks into -- consumed and cleared
# every poll. Deliberately NOT a Telegram command, to avoid touching
# telegram_control.py for this first version.
PICKS_FILE = LOG_DIR / "pending_picks.json"
STATE_FILE = LOG_DIR / "pdf_entry_state.json"

POLL_INTERVAL_SECONDS = 30

# Once both exit GTTs are live, Kite's own server watches the trigger -- no
# polling lag at all for the SL/T1/T2 fire itself, so POLL_INTERVAL_SECONDS
# is plenty for the floor-trail/bookkeeping work this loop still does. But
# before that (waiting for the trader's manual buy to show up in
# kite.positions(), or waiting to place the exit GTTs right after it does),
# there's a real window with NO protection on the position at all -- a much
# shorter poll here shrinks that window a lot, and kite.positions() is a
# light read-only call, well within Kite's rate limits even every few seconds.
FAST_POLL_INTERVAL_SECONDS = 5

# Same tightened trail value the exit-only guardian settled on after its own
# backtest (swing_live.py's TRAILING_PCT) -- reused here as a plain constant,
# not imported, so this file has zero import-time dependency on swing_live.py.
TRAIL_PCT = 0.005

# A GTT's LIMIT leg is placed this much through its trigger so it still fills
# even if price gaps slightly past the trigger before the resting order posts.
GTT_FILL_BUFFER_PCT = 0.003

# Fallback ONLY -- the real per-instrument tick size (see _get_tick_size)
# must be used whenever it's available. NSE requires every order/trigger
# price to be a multiple of the INSTRUMENT'S OWN tick size, which is NOT
# universal: confirmed for real 2026-11-23 that most stocks use 0.05, but
# APOLLOHOSP (a high-priced stock) rejected a GTT with "...multiple of tick
# size 0.50" -- a flat 0.05 assumption is simply wrong for some symbols.
TICK_SIZE = 0.05

# {tradingsymbol: tick_size}, populated once (the whole NSE instrument dump
# comes back in one call anyway, so there's no reason to fetch it per
# symbol) and kept for the life of the process -- tick size essentially
# never changes intraday.
_INSTRUMENT_TICK_CACHE: dict[str, float] = {}


def _get_tick_size(kite, symbol: str) -> float:
    """This instrument's REAL tick size from Kite's own instrument master,
    not a guessed constant. Falls back to TICK_SIZE (0.05, correct for most
    stocks but not all) only if the fetch fails or the symbol genuinely
    isn't found -- better than crashing, but logged nowhere, so a repeat
    failure for the same odd-ticked symbol would keep silently using the
    common-case default. Acceptable: 0.05 is still right most of the time,
    and the alternative (refusing to place any GTT without a confirmed
    tick size) would leave a real position completely unprotected."""
    if symbol in _INSTRUMENT_TICK_CACHE:
        return _INSTRUMENT_TICK_CACHE[symbol]
    try:
        for inst in kite.instruments("NSE"):
            sym = inst.get("tradingsymbol")
            tick = inst.get("tick_size")
            if sym and tick:
                _INSTRUMENT_TICK_CACHE[sym] = float(tick)
    except PLACEMENT_EXCEPTIONS + (KeyError, TypeError, ValueError):
        pass
    return _INSTRUMENT_TICK_CACHE.get(symbol, TICK_SIZE)


def _round_to_tick(price: float, tick: float = TICK_SIZE) -> float:
    return round(round(price / tick) * tick, 2)

PHASE_AWAITING_POSITION = "awaiting_position"
PHASE_AWAITING_EXIT_SETUP = "awaiting_exit_setup"
PHASE_MONITORING = "monitoring"
PHASE_CLOSED = "closed"
PHASE_ERROR = "error"

# --- AUTO-ADOPT sub-flow (2026-11-22) -- a SECOND, independent way a symbol
# enters Part 2, alongside the pick-based flow above. See _scan_for_auto_adopt's
# own docstring for the full mechanics; these are its phases. ---
PHASE_AUTO_WAITING_T1 = "auto_waiting_t1"
PHASE_AUTO_STAGE1_SPLIT = "auto_stage1_split"
PHASE_AUTO_PINNED_T1 = "auto_pinned_t1"
PHASE_AUTO_STAGE2 = "auto_stage2"

# Arm/target offsets ABOVE real breakeven (not raw avg_price) -- explicit
# instruction 2026-11-22, deliberately mirroring swing_live.py's own
# breakeven_pct + PROFIT_TARGET_PCT arm formula (same 2% figure), just
# reimplemented locally so this file still imports nothing from swing_live.py.
AUTO_T1_PCT = 0.02
AUTO_T2_PCT = 0.05

# The "target" leg of every auto-adopt OCO GTT is a placeholder that should
# realistically never fire -- this flow's real sell decisions are always
# made by THIS module raising the SL leg, never by the target leg. Recomputed
# off the CURRENT ltp every time the GTT is replaced, so it's always this
# much further out from wherever price actually is "right now", not stale.
# NOT verified against Kite's own acceptance bounds for a GTT trigger this
# far from LTP (no live credentials in this coding session) -- check the
# first few real ones placed on the VPS actually accept this.
AUTO_PLACEHOLDER_TARGET_MULTIPLE = 1.3

TERMINAL_PHASES = {PHASE_CLOSED, PHASE_ERROR}


def _now() -> datetime:
    return datetime.now(IST)


class PdfEntryLogger:
    """Self-contained jsonl-plus-notifier logger -- same shape as
    swing_live.py's SwingLogger, but its own class, with no import of it."""

    NOTIFY_KINDS = {
        "pdf_entry_picked_up",
        "pdf_entry_position_seen",
        "pdf_entry_exit_gtts_placed",
        "pdf_entry_t1_hit",
        "pdf_entry_t2_hit",
        "pdf_entry_sl_hit",
        "pdf_entry_error",
        "pdf_entry_auto_detected",
        "pdf_entry_auto_armed",
        "pdf_entry_auto_partial_sell",
        "pdf_entry_auto_t2_merge",
        "pdf_entry_auto_t2_reactivate",
        "pdf_entry_auto_remaining_closed",
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
        return f"📥 New pick loaded: {sym} — SL {fields['sl']}, T1 {fields['t1']}, T2 {fields['t2']}. Waiting for your MTF buy to show up."
    if kind == "pdf_entry_position_seen":
        return f"👀 {sym}: your MTF buy is live — qty {fields['qty']} @ ₹{fields['avg_price']:.2f}. Setting up the exit GTTs now."
    if kind == "pdf_entry_exit_gtts_placed":
        return (f"🎯 {sym}: exit GTTs live — {fields['half1_qty']} shares to T1 ₹{fields['t1']}, "
                f"{fields['half2_qty']} shares to T2 ₹{fields['t2']}, both stopped at ₹{fields['sl']}")
    if kind == "pdf_entry_t1_hit":
        return (f"🏆 {sym}: T1 hit — booked {fields['half1_qty']} shares near ₹{fields['t1']:.2f}. "
                f"Remaining {fields['half2_qty']} now floored at ₹{fields['new_floor']:.2f}, trailing toward T2 {fields['t2']}")
    if kind == "pdf_entry_t2_hit":
        return f"🏁 {sym}: T2 reached — remaining {fields['half2_qty']} shares booked near ₹{fields['t2']:.2f}. Position fully closed."
    if kind == "pdf_entry_sl_hit":
        return f"🛑 {sym}: stop-loss hit on {fields['qty']} shares near ₹{fields['price']:.2f}"
    if kind == "pdf_entry_error":
        return f"⚠️ {sym}: {fields.get('message', 'error')}"
    if kind == "pdf_entry_auto_detected":
        return (f"🔎 {sym}: found your own stop-loss (₹{fields['sl']}) on {fields['qty']} shares @ ₹{fields['avg_price']:.2f} -- "
                f"adopting into Part 2, computing T1/T2 off breakeven now.")
    if kind == "pdf_entry_auto_armed":
        return (f"📈 {sym}: armed at T1 ₹{fields['t1']:.2f} -- trailing 0.5% on {fields['half1_qty']}+{fields['half2_qty']} shares "
                f"toward T2 ₹{fields['t2']:.2f}")
    if kind == "pdf_entry_auto_partial_sell":
        return f"✅ {sym}: trailing stop hit before T2 -- sold {fields['qty']} shares near ₹{fields['price']:.2f}. Remaining shares now pinned at T1, waiting."
    if kind == "pdf_entry_auto_t2_merge":
        return f"🏆 {sym}: reached T2 ₹{fields['t2']:.2f} without a pullback -- all {fields['qty']} shares now trailing together, floor ₹{fields['floor']:.2f}"
    if kind == "pdf_entry_auto_t2_reactivate":
        return f"🏆 {sym}: remaining {fields['qty']} shares reached T2 ₹{fields['t2']:.2f} -- trailing resumed, floor ₹{fields['floor']:.2f}"
    if kind == "pdf_entry_auto_remaining_closed":
        return f"🏁 {sym}: {fields['qty']} shares closed near ₹{fields['price']:.2f} ({fields.get('reason', 'stopped out')}). Position fully closed."
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
    """Reads PICKS_FILE (a plain JSON list of {"symbol", "sl", "t1", "t2"}),
    adds any symbol not already in flight into state as 'awaiting_position',
    then clears the file -- so re-running the loop never double-adds the
    same pick. A symbol already in flight (any non-terminal phase) is left
    alone and flagged, not silently overwritten. No buy_low/buy_high/
    capital/quantity here -- the trader buys himself; this only needs the
    exit levels."""
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
            "phase": PHASE_AWAITING_POSITION,
            "sl": float(pick["sl"]),
            "t1": float(pick["t1"]),
            "t2": float(pick["t2"]),
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
        logger.event("pdf_entry_picked_up", symbol=symbol, sl=pick["sl"], t1=pick["t1"], t2=pick["t2"])
    PICKS_FILE.unlink(missing_ok=True)


def _find_mtf_position(kite, symbol: str):
    """The trader's own live MTF position for this symbol, straight from
    kite.positions() -- exactly how the existing swing guardian finds
    positions, just not importing that code. Returns None if nothing's
    showing yet (he hasn't bought it, or it hasn't settled into positions())."""
    try:
        net = kite.positions().get("net", [])
    except PLACEMENT_EXCEPTIONS:
        return None
    for pos in net:
        if pos.get("tradingsymbol") == symbol and pos.get("product") == PRODUCT_MTF and pos.get("quantity", 0) > 0:
            return pos
    return None


def _has_other_active_gtt(kite, symbol: str) -> bool:
    """Safety check: if the trader (or an earlier run) already has an active
    GTT on this symbol, this module must NOT place a second, overlapping
    exit bracket -- it has no way to know which one should win."""
    try:
        all_gtts = kite.get_gtts()
    except PLACEMENT_EXCEPTIONS:
        return False  # can't confirm either way -- proceed, same fail-open as the rest of this poll-based design
    for g in all_gtts:
        condition = g.get("condition", {})
        if condition.get("tradingsymbol") == symbol and g.get("status") == "active":
            return True
    return False


def _get_ltp(kite, symbol: str) -> float | None:
    try:
        q = kite.quote(f"NSE:{symbol}")
        return q[f"NSE:{symbol}"]["last_price"]
    except PLACEMENT_EXCEPTIONS:
        return None


def _funded_amount_for_auto(kite, symbol: str, quantity: int, avg_price: float) -> float | None:
    """Independent reimplementation of swing_live.py's own
    _funded_amount_for_position (same Kite order_margins technique) --
    duplicated here, not imported, so this file keeps zero dependency on
    swing_live.py. Returns None (never raises) on any failure; callers must
    fall back to a flat offset rather than guess a funded amount."""
    try:
        result = kite.order_margins([{
            "exchange": "NSE", "tradingsymbol": symbol, "transaction_type": "BUY",
            "variety": "regular", "product": PRODUCT_MTF, "order_type": "MARKET",
            "quantity": quantity, "price": 0, "trigger_price": 0,
        }])
        required_margin = float(result[0]["total"])
    except PLACEMENT_EXCEPTIONS + (KeyError, IndexError, TypeError, ValueError):
        return None
    position_value = quantity * avg_price
    return max(0.0, position_value - required_margin)


def _breakeven_pct_for_auto(st: dict, today: date) -> float:
    """Same fallback convention as swing_live.py's own _arm_threshold_pct:
    if funded_amount couldn't be determined, skip the breakeven add-on
    entirely (T1/T2 become a flat 2%/5% above avg_price) rather than guess."""
    if st.get("funded_amount") is None:
        return 0.0
    days_held = max(0, (today - date.fromisoformat(st["entry_date"])).days)
    position_value = st["avg_price"] * st["qty"]
    return compute_mtf_breakeven_pct(position_value, st["funded_amount"], days_held)


def _auto_t1_t2(st: dict, today: date) -> tuple[float, float]:
    """T1 = breakeven% + 2%, T2 = breakeven% + 5%, both above avg_price --
    explicit instruction 2026-11-22. Recomputed fresh every poll (not fixed
    at detection time), same reasoning as swing_live.py's own
    _arm_threshold_pct/_trail_pct_for: a carried position's real breakeven
    rises with accrued MTF interest as days_held grows."""
    bep = _breakeven_pct_for_auto(st, today)
    avg = st["avg_price"]
    return avg * (1 + bep + AUTO_T1_PCT), avg * (1 + bep + AUTO_T2_PCT)


def _scan_for_auto_adopt(kite, state: dict[str, dict], logger: PdfEntryLogger) -> None:
    """Looks for a brand-new MTF holding that already carries a self-placed
    PLAIN stop-loss -- a single-leg GTT, SELL side, no target -- the signal
    the trader chose explicitly 2026-11-22: "when I buy a stock, I simply
    fix a stop loss... when you see a stock with a stop loss, it means it
    falls in part 2." No PDF, no chat message, no pending_picks.json entry
    needed at all for this path -- this scan alone is the detector.

    The moment one is found, this converts it into an OCO GTT (same SL,
    plus a far-out placeholder target that should never realistically fire
    -- see AUTO_PLACEHOLDER_TARGET_MULTIPLE) BEFORE adding it to state.
    That conversion is what makes swing_live.py's own fetch_manual_gtt_info
    (which only recognises TWO-LEG GTTs as "manually managed, stand down")
    correctly leave this symbol alone too -- with ZERO changes to that file
    or any other part of the exit-only guardian. If the conversion fails,
    nothing is added to state and the next scan just tries again -- simpler
    and safer than a half-converted retry state sitting unprotected.

    Runs every poll, at whatever the current poll interval happens to be
    (this flow has no PICKS_FILE to signal "something is about to happen",
    so there is no way to pre-emptively fast-poll for the moment the trader
    places his own stop-loss -- the realistic detection lag is up to one
    normal poll interval, same order of magnitude as every other gap this
    module already documents)."""
    try:
        positions = kite.positions().get("net", [])
    except PLACEMENT_EXCEPTIONS:
        return
    try:
        all_gtts = kite.get_gtts()
    except PLACEMENT_EXCEPTIONS:
        return

    single_leg_sl: dict[str, dict] = {}
    for g in all_gtts:
        if g.get("status") != "active" or g.get("type") != "single":
            continue
        cond = g.get("condition") or {}
        symbol = cond.get("tradingsymbol")
        values = cond.get("trigger_values") or []
        order_legs = g.get("orders") or []
        if not symbol or len(values) != 1 or len(order_legs) != 1:
            continue
        if order_legs[0].get("transaction_type") != "SELL":
            continue
        single_leg_sl[symbol] = {"trigger_id": g.get("id"), "sl": float(values[0])}

    for pos in positions:
        symbol = pos.get("tradingsymbol")
        if pos.get("product") != PRODUCT_MTF or (pos.get("quantity") or 0) <= 0:
            continue
        existing = state.get(symbol)
        if existing and existing["phase"] not in TERMINAL_PHASES:
            continue  # already tracked (pick-based or auto) -- leave it alone
        gtt_info = single_leg_sl.get(symbol)
        if gtt_info is None:
            continue  # no self-placed plain stop-loss -- not an auto-adopt candidate

        qty = pos["quantity"]
        avg_price = pos["average_price"]
        ltp = _get_ltp(kite, symbol) or avg_price
        placeholder = ltp * AUTO_PLACEHOLDER_TARGET_MULTIPLE
        try:
            new_gtt = _place_oco_gtt(kite, symbol, qty, gtt_info["sl"], placeholder, ltp)
        except PLACEMENT_EXCEPTIONS as exc:
            logger.event("pdf_entry_error", symbol=symbol,
                         message=f"found a self-placed stop-loss but could not convert it to a standdown bracket: {exc}")
            continue
        try:
            kite.delete_gtt(gtt_info["trigger_id"])
        except PLACEMENT_EXCEPTIONS:
            pass  # the old single-leg GTT may already be gone -- the new OCO above is what matters

        state[symbol] = {
            "source": "auto",
            "phase": PHASE_AUTO_WAITING_T1,
            "sl": gtt_info["sl"],
            "qty": qty,
            "avg_price": avg_price,
            "funded_amount": _funded_amount_for_auto(kite, symbol, qty, avg_price),
            "entry_date": _now().date().isoformat(),
            "gtt_id": new_gtt,
        }
        logger.event("pdf_entry_auto_detected", symbol=symbol, sl=gtt_info["sl"], qty=qty, avg_price=avg_price)


def _advance_awaiting_position(kite, symbol: str, st: dict, logger: PdfEntryLogger) -> None:
    pos = _find_mtf_position(kite, symbol)
    if pos is None:
        return  # he hasn't bought it yet (or it hasn't shown up in positions() yet) -- keep waiting
    if _has_other_active_gtt(kite, symbol):
        st["phase"] = PHASE_ERROR
        logger.event("pdf_entry_error", symbol=symbol,
                     message="an active GTT already exists on this symbol -- not placing a second one. Check Kite and handle manually.")
        return
    st["qty"] = pos["quantity"]
    st["avg_price"] = pos["average_price"]
    st["phase"] = PHASE_AWAITING_EXIT_SETUP
    logger.event("pdf_entry_position_seen", symbol=symbol, qty=st["qty"], avg_price=st["avg_price"])


def _place_oco_gtt(kite, symbol: str, qty: int, sl: float, target: float, ltp: float):
    tick = _get_tick_size(kite, symbol)
    sl = _round_to_tick(sl, tick)
    target = _round_to_tick(target, tick)
    sl_price = _round_to_tick(sl * (1 - GTT_FILL_BUFFER_PCT), tick)
    target_price = _round_to_tick(target * (1 - GTT_FILL_BUFFER_PCT), tick)
    resp = kite.place_gtt(
        trigger_type=kite.GTT_TYPE_OCO,
        tradingsymbol=symbol,
        exchange=kite.EXCHANGE_NSE,
        trigger_values=[sl, target],
        last_price=ltp,
        orders=[
            {"transaction_type": "SELL", "quantity": qty, "order_type": kite.ORDER_TYPE_LIMIT, "product": PRODUCT_MTF, "price": sl_price},
            {"transaction_type": "SELL", "quantity": qty, "order_type": kite.ORDER_TYPE_LIMIT, "product": PRODUCT_MTF, "price": target_price},
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
        gtt2_id = _place_oco_gtt(kite, symbol, half2, st["sl"], st["t2"], ltp)
        gtt1_id = _place_oco_gtt(kite, symbol, half1, st["sl"], st["t1"], ltp) if half1 else None
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


def _find_recent_sell_fill(kite, symbol: str, qty: int):
    try:
        all_orders = kite.orders()
    except PLACEMENT_EXCEPTIONS:
        return None
    for o in reversed(all_orders):
        if (o.get("tradingsymbol") == symbol and o.get("transaction_type") == "SELL"
                and o.get("product") == PRODUCT_MTF and o.get("status") == "COMPLETE"
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
        st["gtt2_id"] = _place_oco_gtt(kite, symbol, st["half2_qty"], new_floor, st["t2"], ltp)
        st["gtt2_floor"] = new_floor
    except PLACEMENT_EXCEPTIONS as exc:
        logger.event("pdf_entry_error", symbol=symbol, message=f"could not raise floor to {new_floor} ({reason}): {exc}")


def _advance_auto_waiting_t1(kite, symbol: str, st: dict, logger: PdfEntryLogger) -> None:
    """Waiting for price to reach T1 (breakeven% + 2%). The standdown OCO
    placed at detection (SL = the trader's own stop, target = placeholder)
    is left completely untouched until then -- nothing to do but watch."""
    ltp = _get_ltp(kite, symbol)
    if ltp is None:
        return
    today = _now().date()
    t1, t2 = _auto_t1_t2(st, today)
    if ltp < t1:
        return

    qty = st["qty"]
    placeholder = ltp * AUTO_PLACEHOLDER_TARGET_MULTIPLE
    if qty == 1:
        # Can't split a single share -- skip straight to a single trailing
        # bracket covering the whole (1-share) position, floored at T1.
        try:
            new_gtt = _place_oco_gtt(kite, symbol, 1, t1, placeholder, ltp)
        except PLACEMENT_EXCEPTIONS as exc:
            logger.event("pdf_entry_error", symbol=symbol, message=f"could not arm trailing at T1: {exc}")
            return
        try:
            kite.delete_gtt(st["gtt_id"])
        except PLACEMENT_EXCEPTIONS:
            pass
        st["gtt_id"] = new_gtt
        st["gtt_qty"] = 1
        st["floor"] = t1
        st["peak"] = ltp
        st["phase"] = PHASE_AUTO_STAGE2
        logger.event("pdf_entry_auto_armed", symbol=symbol, t1=t1, t2=t2, half1_qty=0, half2_qty=1)
        return

    half1 = qty // 2
    half2 = qty - half1
    try:
        gttA = _place_oco_gtt(kite, symbol, half1, t1, placeholder, ltp)
        gttB = _place_oco_gtt(kite, symbol, half2, t1, placeholder, ltp)
    except PLACEMENT_EXCEPTIONS as exc:
        logger.event("pdf_entry_error", symbol=symbol, message=f"could not arm trailing at T1: {exc}")
        return
    try:
        kite.delete_gtt(st["gtt_id"])
    except PLACEMENT_EXCEPTIONS:
        pass
    st["gttA_id"] = gttA
    st["gttA_qty"] = half1
    st["gttA_floor"] = t1
    st["gttB_id"] = gttB
    st["gttB_qty"] = half2
    st["peak"] = max(t1, ltp)
    st["phase"] = PHASE_AUTO_STAGE1_SPLIT
    logger.event("pdf_entry_auto_armed", symbol=symbol, t1=t1, t2=t2, half1_qty=half1, half2_qty=half2)


def _advance_auto_stage1_split(kite, symbol: str, st: dict, logger: PdfEntryLogger) -> None:
    """Between T1 and T2: GTT A (half the qty) actively trails 0.5% below
    peak; GTT B (the other half) sits pinned at T1, untouched. Whichever
    fires first decides what happens next -- see this module's own
    docstring for the full state machine."""
    today = _now().date()
    t1, t2 = _auto_t1_t2(st, today)

    gttA = _gtt_status(kite, st["gttA_id"])
    if gttA is not None and gttA.get("status") == kite.GTT_STATUS_TRIGGERED:
        fill = _find_recent_sell_fill(kite, symbol, st["gttA_qty"])
        price = fill.get("average_price") if fill else st["gttA_floor"]
        logger.event("pdf_entry_auto_partial_sell", symbol=symbol, qty=st["gttA_qty"], price=price)
        st["gttA_id"] = None
        st["phase"] = PHASE_AUTO_PINNED_T1
        return

    gttB = _gtt_status(kite, st["gttB_id"])
    if gttB is not None and gttB.get("status") == kite.GTT_STATUS_TRIGGERED:
        # Defensive only -- a severe gap took the PINNED-at-T1 half out
        # before GTT A's own (higher) trailing floor ever fired. Treat the
        # whole position as closed; GTT A is cancelled, not filled.
        fill = _find_recent_sell_fill(kite, symbol, st["gttB_qty"])
        price = fill.get("average_price") if fill else t1
        try:
            kite.delete_gtt(st["gttA_id"])
        except PLACEMENT_EXCEPTIONS:
            pass
        logger.event("pdf_entry_auto_remaining_closed", symbol=symbol, qty=st["qty"], price=price, reason="gap through T1")
        st["phase"] = PHASE_CLOSED
        return

    ltp = _get_ltp(kite, symbol)
    if ltp is None:
        return
    st["peak"] = max(st.get("peak", t1), ltp)

    if st["peak"] >= t2:
        # Reached T2 without ever pulling back -- merge the two halves back
        # into ONE full-qty trailing bracket, exactly per the trader's
        # clarification: no partial sale in this path, a single future exit.
        total_qty = st["gttA_qty"] + st["gttB_qty"]
        new_floor = max(t2, st["gttA_floor"])
        placeholder = ltp * AUTO_PLACEHOLDER_TARGET_MULTIPLE
        try:
            merged = _place_oco_gtt(kite, symbol, total_qty, new_floor, placeholder, ltp)
        except PLACEMENT_EXCEPTIONS as exc:
            logger.event("pdf_entry_error", symbol=symbol, message=f"could not merge into full trailing at T2: {exc}")
            return
        for gid in (st["gttA_id"], st["gttB_id"]):
            try:
                kite.delete_gtt(gid)
            except PLACEMENT_EXCEPTIONS:
                pass
        st["gtt_id"] = merged
        st["gtt_qty"] = total_qty
        st["floor"] = new_floor
        st["phase"] = PHASE_AUTO_STAGE2
        logger.event("pdf_entry_auto_t2_merge", symbol=symbol, qty=total_qty, floor=new_floor, t2=t2)
        return

    new_floor = max(st["gttA_floor"], st["peak"] * (1 - TRAIL_PCT))
    if new_floor > st["gttA_floor"]:
        placeholder = ltp * AUTO_PLACEHOLDER_TARGET_MULTIPLE
        try:
            kite.delete_gtt(st["gttA_id"])
            st["gttA_id"] = _place_oco_gtt(kite, symbol, st["gttA_qty"], new_floor, placeholder, ltp)
            st["gttA_floor"] = new_floor
        except PLACEMENT_EXCEPTIONS as exc:
            logger.event("pdf_entry_error", symbol=symbol, message=f"could not raise stage-1 trailing floor: {exc}")


def _advance_auto_pinned_t1(kite, symbol: str, st: dict, logger: PdfEntryLogger) -> None:
    """The remaining half, parked statically at T1 after the first partial
    sale -- not trailing yet. Either it gets stopped out right here (a solid
    win either way, since half was already booked at T1), or price reaches
    T2 and trailing resumes."""
    today = _now().date()
    t1, t2 = _auto_t1_t2(st, today)
    gttB = _gtt_status(kite, st["gttB_id"])
    if gttB is not None and gttB.get("status") == kite.GTT_STATUS_TRIGGERED:
        fill = _find_recent_sell_fill(kite, symbol, st["gttB_qty"])
        price = fill.get("average_price") if fill else t1
        logger.event("pdf_entry_auto_remaining_closed", symbol=symbol, qty=st["gttB_qty"], price=price, reason="pinned at T1, stopped out")
        st["gttB_id"] = None
        st["phase"] = PHASE_CLOSED
        return
    ltp = _get_ltp(kite, symbol)
    if ltp is None:
        return
    if ltp >= t2:
        placeholder = ltp * AUTO_PLACEHOLDER_TARGET_MULTIPLE
        try:
            new_gtt = _place_oco_gtt(kite, symbol, st["gttB_qty"], t1, placeholder, ltp)
        except PLACEMENT_EXCEPTIONS as exc:
            logger.event("pdf_entry_error", symbol=symbol, message=f"could not reactivate trailing past T2: {exc}")
            return
        try:
            kite.delete_gtt(st["gttB_id"])
        except PLACEMENT_EXCEPTIONS:
            pass
        st["gtt_id"] = new_gtt
        st["gtt_qty"] = st["gttB_qty"]
        st["floor"] = t1
        st["peak"] = ltp
        st["phase"] = PHASE_AUTO_STAGE2
        logger.event("pdf_entry_auto_t2_reactivate", symbol=symbol, qty=st["gttB_qty"], floor=t1, t2=t2)


def _advance_auto_stage2(kite, symbol: str, st: dict, logger: PdfEntryLogger) -> None:
    """One GTT, continuously raised 0.5% below peak -- covers BOTH 'ran
    straight through T2' (full original qty) and 'already took a partial
    exit, now past T2 too' (the remaining half) identically; st["gtt_qty"]
    and st["floor"] already encode which case this is."""
    gtt = _gtt_status(kite, st["gtt_id"])
    if gtt is not None and gtt.get("status") == kite.GTT_STATUS_TRIGGERED:
        fill = _find_recent_sell_fill(kite, symbol, st["gtt_qty"])
        price = fill.get("average_price") if fill else st["floor"]
        logger.event("pdf_entry_auto_remaining_closed", symbol=symbol, qty=st["gtt_qty"], price=price, reason="stage-2 trailing stop")
        st["gtt_id"] = None
        st["phase"] = PHASE_CLOSED
        return
    ltp = _get_ltp(kite, symbol)
    if ltp is None:
        return
    st["peak"] = max(st.get("peak", st["floor"]), ltp)
    new_floor = max(st["floor"], st["peak"] * (1 - TRAIL_PCT))
    if new_floor > st["floor"]:
        placeholder = ltp * AUTO_PLACEHOLDER_TARGET_MULTIPLE
        try:
            kite.delete_gtt(st["gtt_id"])
            st["gtt_id"] = _place_oco_gtt(kite, symbol, st["gtt_qty"], new_floor, placeholder, ltp)
            st["floor"] = new_floor
        except PLACEMENT_EXCEPTIONS as exc:
            logger.event("pdf_entry_error", symbol=symbol, message=f"could not raise stage-2 trailing floor: {exc}")


def _advance_monitoring(kite, symbol: str, st: dict, logger: PdfEntryLogger) -> None:
    # --- half1 (T1 bracket), only if it exists ---
    if st["gtt1_id"] and st["half1_qty"]:
        gtt1 = _gtt_status(kite, st["gtt1_id"])
        if gtt1 is not None and gtt1.get("status") == kite.GTT_STATUS_TRIGGERED:
            fill = _find_recent_sell_fill(kite, symbol, st["half1_qty"])
            price = fill.get("average_price") if fill else st["t1"]
            hit_target = price is not None and abs(price - st["t1"]) <= abs(price - st["sl"])
            if hit_target:
                # Floor goes to the ORIGINAL BUY PRICE (breakeven), not T1 -- explicit
                # instruction 2026-11-22: "protect my capital", not lock in the T1-level
                # profit on the remaining half. Trailing (below) still applies from here
                # onward, same as before -- this only changes where the floor STARTS.
                new_floor = max(st["gtt2_floor"], st["avg_price"])
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
        fill = _find_recent_sell_fill(kite, symbol, st["half2_qty"])
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


def check_once(kite, state: dict[str, dict], logger: PdfEntryLogger) -> bool:
    """Returns True if any tracked symbol is still unprotected (waiting for
    the manual buy to appear, or waiting to get its exit GTTs placed) -- the
    caller uses this to decide whether to poll again at FAST_POLL_INTERVAL_SECONDS
    instead of the normal POLL_INTERVAL_SECONDS (see run_pdf_entry_bot)."""
    _consume_pending_picks(state, logger)
    _scan_for_auto_adopt(kite, state, logger)
    needs_fast_poll = False
    for symbol, st in list(state.items()):
        phase = st["phase"]
        if phase == PHASE_AWAITING_POSITION:
            _advance_awaiting_position(kite, symbol, st, logger)
            if st["phase"] in (PHASE_AWAITING_POSITION, PHASE_AWAITING_EXIT_SETUP):
                needs_fast_poll = True
        elif phase == PHASE_AWAITING_EXIT_SETUP:
            _advance_awaiting_exit_setup(kite, symbol, st, logger)
            if st["phase"] == PHASE_AWAITING_EXIT_SETUP:
                needs_fast_poll = True
        elif phase == PHASE_MONITORING:
            _advance_monitoring(kite, symbol, st, logger)
        elif phase == PHASE_AUTO_WAITING_T1:
            _advance_auto_waiting_t1(kite, symbol, st, logger)
        elif phase == PHASE_AUTO_STAGE1_SPLIT:
            _advance_auto_stage1_split(kite, symbol, st, logger)
        elif phase == PHASE_AUTO_PINNED_T1:
            _advance_auto_pinned_t1(kite, symbol, st, logger)
        elif phase == PHASE_AUTO_STAGE2:
            _advance_auto_stage2(kite, symbol, st, logger)
    save_state(state)
    return needs_fast_poll


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
            "separate safety gate for the PDF/pick exit-automation bot -- add it explicitly "
            "if you actually intend this to place real exit GTTs."
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

        needs_fast_poll = False
        try:
            needs_fast_poll = check_once(kite, state, logger)
        except Exception as exc:  # noqa: BLE001 - one bad tick must never kill the whole process
            logger.event("pdf_entry_error", symbol="ALL", message=f"check_once failed: {exc!r}")
        time_module.sleep(FAST_POLL_INTERVAL_SECONDS if needs_fast_poll else poll_interval)
