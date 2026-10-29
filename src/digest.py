"""Periodic phone summary: holding net P&L (swing/MTF) and today's intraday
move (manual MIS positions), sent at fixed clock times during market hours.

Explicit request: "every 3 hours during market hour starting from 10 am ...
about the holding net p&l, todays intraday move". Fires at DIGEST_TIMES only
-- it does not replace the instant alerts (trailing armed, sold, +-3% move),
which fire from swing_live.py/intraday_live.py the moment they happen.

Independent of the guardians' own in-memory tracking state on purpose: it
reads positions straight from Kite each time, so a digest is never stale or
missing something the guardian hasn't polled yet, and a guardian crash can't
take the digest down with it.
"""

import json
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from . import orders
from .swing_live import SWING_STATE_FILE

IST = ZoneInfo("Asia/Kolkata")

# Every 3 hours starting 10:00, for as long as that still falls inside market
# hours (9:15-15:30) -- 10:00 and 13:00; 16:00 would be past close, so it
# never fires. Change this list directly if the cadence should differ.
DIGEST_TIMES = [time(10, 0), time(13, 0)]


def _money(x) -> str:
    return f"₹{x:,.2f}"


def _trailing_armed_symbols() -> set[str]:
    """Best-effort read of swing_live's own persisted state, for the "is
    trailing already armed" flag only -- everything else in this digest
    comes straight from Kite (see module docstring on why). A read failure
    (file mid-write, missing, or malformed) just means the flag is omitted
    for this one digest, never a reason to fail the whole message."""
    try:
        with open(SWING_STATE_FILE) as f:
            state = json.load(f)
        return {sym for sym, st in state.items() if st.get("trailing")}
    except Exception:  # noqa: BLE001
        return set()


def _swing_section(kite, today: date) -> tuple[str, float, bool]:
    """Returns (text, total_gross_pnl, any_position). GROSS only, same
    convention as swing_live's own swing_exit log -- the real MTF funding
    interest depends on entry_date and funded_amount, which live in
    swing_live's own tracking state, not in Kite's raw position response,
    and this is deliberately independent of that state (see module
    docstring). Ask for a specific symbol's net figure on demand instead
    (same approach already used for a one-off real exit)."""
    try:
        net = kite.positions().get("net", [])
    except Exception as exc:  # noqa: BLE001 - a fetch failure must not break the whole digest
        return f"  (couldn't fetch swing holdings: {exc})", 0.0, False
    mtf = [p for p in net if p.get("product") == orders.PRODUCT_MTF and p.get("quantity", 0) > 0]
    if not mtf:
        return "  No open swing (MTF) holdings.", 0.0, False

    armed = _trailing_armed_symbols()
    symbols = [p["tradingsymbol"] for p in mtf]
    try:
        quotes = kite.quote([f"NSE:{s}" for s in symbols])
    except Exception:
        quotes = {}

    lines = []
    total_gross = 0.0
    for p in mtf:
        sym = p["tradingsymbol"]
        q = quotes.get(f"NSE:{sym}")
        ltp = q.get("last_price") if q else None
        qty, avg = p["quantity"], p["average_price"]
        arm_tag = " [trailing armed]" if sym in armed else ""
        if ltp is None:
            lines.append(f"  {sym}: {qty} @ {_money(avg)} -- live price unavailable{arm_tag}")
            continue
        gross = (ltp - avg) * qty
        total_gross += gross
        pct = (ltp - avg) / avg * 100
        lines.append(f"  {sym}: {qty} @ {_money(avg)} -> {_money(ltp)} ({pct:+.2f}%), P&L {_money(gross)} (gross){arm_tag}")
    return "\n".join(lines), total_gross, True


def _intraday_section(kite) -> tuple[str, float, bool]:
    try:
        day = kite.positions().get("day", [])
    except Exception as exc:  # noqa: BLE001
        return f"  (couldn't fetch intraday positions: {exc})", 0.0, False
    mis = [p for p in day if p.get("product") == "MIS" and p.get("quantity", 0) != 0]
    if not mis:
        return "  No open intraday (MIS) positions right now.", 0.0, False

    symbols = [p["tradingsymbol"] for p in mis]
    try:
        quotes = kite.quote([f"NSE:{s}" for s in symbols])
    except Exception:
        quotes = {}

    lines = []
    total_gross = 0.0
    for p in mis:
        sym = p["tradingsymbol"]
        q = quotes.get(f"NSE:{sym}")
        ltp = q.get("last_price") if q else None
        qty, avg = p["quantity"], p["average_price"]
        direction = "long" if qty > 0 else "short"
        if ltp is None:
            lines.append(f"  {sym} ({direction}): {abs(qty)} @ {_money(avg)} -- live price unavailable")
            continue
        gross = (ltp - avg) * qty  # already direction-correct: qty is negative for a short
        total_gross += gross
        pct = (ltp - avg) / avg * 100 * (1 if direction == "long" else -1)
        lines.append(f"  {sym} ({direction}): {abs(qty)} @ {_money(avg)} -> {_money(ltp)} ({pct:+.2f}%), P&L {_money(gross)}")
    return "\n".join(lines), total_gross, True


def build_digest_message(kite) -> str:
    now = datetime.now(IST)
    swing_text, swing_total, has_swing = _swing_section(kite, now.date())
    intraday_text, intraday_total, has_intraday = _intraday_section(kite)
    grand_total = swing_total + intraday_total

    lines = [f"📊 Portfolio check-in — {now.strftime('%H:%M')} IST", "", "Swing (MTF) holdings:", swing_text,
              "", "Today's intraday (MIS):", intraday_text]
    if has_swing or has_intraday:
        lines += ["", f"Combined open P&L (gross): {_money(grand_total)}"]
    return "\n".join(lines)


def run_digest_loop(get_kite, notifier, should_stop=None, poll_interval: int = 30) -> None:
    """Sends build_digest_message() once at each time in DIGEST_TIMES, once
    per calendar day. Meant to run as its own daemon thread alongside the
    guardians (see scripts/run_all_guardians.py) -- entirely independent of
    their state, so it can't be broken by a guardian restart, and a digest
    failure (logged, not raised) can't take a guardian down either.

    `sent_today` is in-memory only: a process restart after a digest time has
    already passed today will NOT re-send it (so a crash-restart loop can't
    spam duplicates), but will also not catch up a MISSED digest from before
    the restart. Given the guardians are meant to stay up all day, this is an
    acceptable trade-off for how rarely a restart should actually happen.
    """
    import time as time_module

    sent_today: set[tuple[date, time]] = set()
    while True:
        if should_stop is not None and should_stop():
            return
        now = datetime.now(IST)
        for t in DIGEST_TIMES:
            key = (now.date(), t)
            if now.time() >= t and key not in sent_today:
                sent_today.add(key)
                try:
                    kite = get_kite()
                    message = build_digest_message(kite)
                    if notifier:
                        notifier(message)
                except Exception as exc:  # noqa: BLE001 - one bad digest must never stop future ones
                    print(f"WARNING: digest at {t} failed: {exc}")
        time_module.sleep(poll_interval)
