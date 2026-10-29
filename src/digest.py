"""End-of-day phone table: buy price, current price, and real NET P&L for
every open swing (MTF) holding, sent once at EOD_TABLE_TIME.

Replaces an earlier twice-daily (10:00/13:00) gross-P&L digest that turned
out to be clutter -- the explicit ask now is exactly one table, at 4:01 PM,
with real net figures, and nothing in between. The three INSTANT alerts
(trailing armed, sold, +-3% move) are unaffected -- they still fire from
swing_live.py/intraday_live.py the moment they happen; this module only
covers the one daily table.

Net P&L needs funded_amount and entry_date per holding, which only
swing_live's own persisted state (SWING_STATE_FILE) has -- Kite's raw
position response doesn't carry either. So, unlike an earlier draft of this
module, the table is NOT independent of that state: a holding missing from
the state file (e.g. the guardian hasn't polled it yet this run) is still
listed, just with gross P&L and a note, rather than silently dropped.
"""

import json
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from . import orders
from .swing_live import SWING_STATE_FILE
from .swing_strategy import MTF_PLEDGE_CHARGE, mtf_interest, mtf_order_cost

IST = ZoneInfo("Asia/Kolkata")

EOD_TABLE_TIME = time(16, 1)  # 4:01 PM, after the 15:30 close


def _money(x) -> str:
    return f"{x:,.2f}"


def _escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _load_swing_state() -> dict:
    try:
        with open(SWING_STATE_FILE) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 - missing/mid-write/malformed -> treat as "no extra info available"
        return {}


def _net_pnl(position_value: float, funded_amount: float, days_held: int, gross: float) -> float:
    """Same cost model as the real swing_exit figure (see swing_live.py),
    applied to the CURRENT unrealized gross as if sold right now."""
    buy_cost = mtf_order_cost(position_value, "buy")
    sell_cost = mtf_order_cost(position_value + gross, "sell")
    interest = mtf_interest(funded_amount, days_held)
    pledge_roundtrip = 2 * MTF_PLEDGE_CHARGE
    return gross - (buy_cost + sell_cost + interest + pledge_roundtrip)


def build_eod_table(kite) -> str | None:
    """Returns the HTML-ready message, or None if there is nothing open to
    show (no message is sent that day in that case)."""
    today = datetime.now(IST).date()
    try:
        net = kite.positions().get("net", [])
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ End-of-day table failed -- couldn't fetch holdings: {exc}"
    mtf = [p for p in net if p.get("product") == orders.PRODUCT_MTF and p.get("quantity", 0) > 0]
    if not mtf:
        return None

    state = _load_swing_state()
    symbols = [p["tradingsymbol"] for p in mtf]
    try:
        quotes = kite.quote([f"NSE:{s}" for s in symbols])
    except Exception:
        quotes = {}

    rows = []  # (symbol, buy, current, net_pnl_str, armed_flag)
    total_net = 0.0
    total_known = True
    for p in mtf:
        sym = p["tradingsymbol"]
        qty, avg = p["quantity"], p["average_price"]
        q = quotes.get(f"NSE:{sym}")
        ltp = q.get("last_price") if q else None
        st = state.get(sym)
        armed = " *" if st and st.get("trailing") else ""

        if ltp is None:
            rows.append((sym, _money(avg), "n/a", "n/a" + armed))
            total_known = False
            continue

        gross = (ltp - avg) * qty
        funded_amount = st.get("funded_amount") if st else None
        entry_date_str = st.get("entry_date") if st else None
        if funded_amount is not None and entry_date_str:
            days_held = max(0, (today - date.fromisoformat(entry_date_str)).days)
            net_pnl = _net_pnl(qty * avg, funded_amount, days_held, gross)
            total_net += net_pnl
            rows.append((sym, _money(avg), _money(ltp), _money(net_pnl) + armed))
        else:
            # No funded_amount/entry_date on record (e.g. detected this instant, before the
            # guardian's own first poll saved it) -- show gross rather than silently guess
            # a net figure, and flag the total as incomplete.
            total_known = False
            rows.append((sym, _money(avg), _money(ltp), _money(gross) + " (gross)" + armed))

    name_w = max(6, max(len(r[0]) for r in rows))
    header = f"{'Symbol':<{name_w}}  {'Buy':>10}  {'Current':>10}  {'Net P&L':>14}"
    sep = "-" * len(header)
    lines = [header, sep]
    for sym, buy, cur, pnl in rows:
        lines.append(f"{sym:<{name_w}}  {buy:>10}  {cur:>10}  {pnl:>14}")
    table = _escape_html("\n".join(lines))

    total_line = f"TOTAL net P&L: ₹{_money(total_net)}" if total_known else \
        f"TOTAL net P&L (where known): ₹{_money(total_net)} -- one or more rows above are gross/n-a, see note"
    armed_note = "  * = trailing already armed" if any("*" in r[3] for r in rows) else ""

    return (f"📋 End-of-day holdings — {today.isoformat()}\n\n"
            f"<pre>{table}</pre>\n"
            f"{total_line}{armed_note}")


def run_digest_loop(get_kite, notifier, should_stop=None, poll_interval: int = 30) -> None:
    """Sends build_eod_table() once, at EOD_TABLE_TIME, once per calendar
    day. Meant to run as its own daemon thread alongside the guardians (see
    scripts/run_all_guardians.py) -- a failure here is logged, never raised,
    so it can't take a guardian down, and a guardian crash/restart can't
    break this loop either.

    `sent_today` is in-memory only: a process restart after EOD_TABLE_TIME
    has already passed today will NOT re-send it today (so a crash-restart
    loop can't spam duplicates), but also won't catch up a table missed
    before the restart -- an acceptable trade-off given how rarely that
    should actually happen with the supervisor keeping this process up.
    """
    import time as time_module

    sent_today: set[date] = set()
    while True:
        if should_stop is not None and should_stop():
            return
        now = datetime.now(IST)
        if now.time() >= EOD_TABLE_TIME and now.date() not in sent_today:
            sent_today.add(now.date())
            try:
                kite = get_kite()
                message = build_eod_table(kite)
                if message and notifier:
                    notifier(message, parse_mode="HTML")
            except Exception as exc:  # noqa: BLE001 - one bad day must never stop future ones
                print(f"WARNING: end-of-day table failed: {exc}")
        time_module.sleep(poll_interval)
