"""End-of-day phone table, sent once at EOD_TABLE_TIME: every open swing
(MTF) holding and every open intraday (MIS) position with buy/current/P&L
and trailing-armed status, plus a recap of everything actually SOLD today.

Replaces an earlier twice-daily (10:00/13:00) gross-P&L digest that turned
out to be clutter -- the explicit ask now is exactly one table, at 4:01 PM.
The three INSTANT alerts (trailing armed, sold, +-3% move) are unaffected --
they still fire from swing_live.py/intraday_live.py the moment they happen;
this module only covers the one daily report.

Swing net P&L needs funded_amount and entry_date per holding, which only
swing_live's own persisted state (SWING_STATE_FILE) has -- Kite's raw
position response doesn't carry either. Intraday has no equivalent
persisted state (MIS resets daily, by design -- see intraday_live.py's own
docstring), so "is trailing armed" for an open intraday position, and the
full sold-today recap for BOTH guardians, are reconstructed by replaying
today's own JSONL log files rather than depending on either guardian's
live in-memory state -- a guardian crash/restart can't break this report,
and it can't break a guardian either (every failure here is caught, never
raised).
"""

import json
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from . import orders
from .swing_live import SWING_STATE_FILE
from .swing_strategy import MTF_PLEDGE_CHARGE, mtf_interest, mtf_order_cost

IST = ZoneInfo("Asia/Kolkata")
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

EOD_TABLE_TIME = time(16, 1)  # 4:01 PM, after the 15:30 close

# A FILE, not just stdout -- found the hard way on 2026-10-29: this thread shares a
# terminal with swing_live's own warnings, which can repeat every poll (a books-mismatch
# warning, say) and fill tmux's limited scrollback within hours, silently erasing whatever
# this thread printed hours earlier. A persistent line here survives that regardless of
# how noisy the terminal gets, so a missed/failed table is actually diagnosable the next day.
DIGEST_LOG_FILE = LOG_DIR / "digest.log"


def _log(line: str) -> None:
    stamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    text = f"{stamp} {line}"
    print(text)
    try:
        DIGEST_LOG_FILE.parent.mkdir(exist_ok=True)
        with open(DIGEST_LOG_FILE, "a") as f:
            f.write(text + "\n")
    except Exception as exc:  # noqa: BLE001 - a logging failure must never break the digest itself
        print(f"WARNING: could not write {DIGEST_LOG_FILE}: {exc}")


def _money(x) -> str:
    return f"{x:,.2f}"


def _escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _todays_log_records(glob_pattern: str, today_str: str) -> list[dict]:
    """Every JSON record from logs/<glob_pattern> whose own timestamp falls
    on today_str (YYYY-MM-DD) -- NOT just files whose NAME matches today,
    since a guardian that's been running since before midnight still writes
    into yesterday's-named file (see swing_live.py's log-file-per-run
    design). A malformed line or unreadable file is skipped, never raised."""
    records = []
    for path in LOG_DIR.glob(glob_pattern):
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("timestamp", "").startswith(today_str):
                        records.append(rec)
        except Exception:  # noqa: BLE001 - one bad file must never break the whole report
            continue
    records.sort(key=lambda r: r.get("timestamp", ""))
    return records


def _load_swing_state() -> dict:
    try:
        with open(SWING_STATE_FILE) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 - missing/mid-write/malformed -> treat as "no extra info available"
        return {}


def _intraday_armed_symbols(today_str: str) -> set[str]:
    """Intraday has no persisted state file (MIS resets daily on purpose),
    so "is trailing armed right now" is reconstructed by replaying today's
    own events in order: armed on intraday_trailing_armed, cleared on any
    exit/square-off/external-close for that symbol."""
    armed: set[str] = set()
    for rec in _todays_log_records("intraday_manual_*.jsonl", today_str):
        kind, sym = rec.get("kind"), rec.get("symbol")
        if kind == "intraday_trailing_armed":
            armed.add(sym)
        elif kind in ("intraday_exit", "intraday_square_off", "intraday_position_closed_externally"):
            armed.discard(sym)
    return armed


def _net_pnl(position_value: float, funded_amount: float, days_held: int, gross: float) -> float:
    """Same cost model as the real swing_exit figure (see swing_live.py),
    applied to the CURRENT unrealized gross as if sold right now."""
    buy_cost = mtf_order_cost(position_value, "buy")
    sell_cost = mtf_order_cost(position_value + gross, "sell")
    interest = mtf_interest(funded_amount, days_held)
    pledge_roundtrip = 2 * MTF_PLEDGE_CHARGE
    return gross - (buy_cost + sell_cost + interest + pledge_roundtrip)


def _render_table(rows: list[tuple[str, str, str, str]]) -> str:
    """rows: (symbol, buy, current, pnl_str). Returns HTML-escaped,
    <pre>-ready monospace text (caller wraps it in <pre>...</pre>)."""
    name_w = max(6, max(len(r[0]) for r in rows))
    header = f"{'Symbol':<{name_w}}  {'Buy':>10}  {'Current':>10}  {'P&L':>16}"
    sep = "-" * len(header)
    lines = [header, sep]
    for sym, buy, cur, pnl in rows:
        lines.append(f"{sym:<{name_w}}  {buy:>10}  {cur:>10}  {pnl:>16}")
    return _escape_html("\n".join(lines))


def _swing_section(kite, today: date) -> str:
    try:
        net = kite.positions().get("net", [])
    except Exception as exc:  # noqa: BLE001
        return f"(couldn't fetch swing holdings: {exc})"
    mtf = [p for p in net if p.get("product") == orders.PRODUCT_MTF and p.get("quantity", 0) > 0]
    if not mtf:
        return "No open swing (MTF) holdings."

    state = _load_swing_state()
    symbols = [p["tradingsymbol"] for p in mtf]
    try:
        quotes = kite.quote([f"NSE:{s}" for s in symbols])
    except Exception:
        quotes = {}

    rows = []
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

    table = _render_table(rows)
    total_line = f"Swing TOTAL net P&L: ₹{_money(total_net)}" if total_known else \
        f"Swing TOTAL net P&L (where known): ₹{_money(total_net)} -- one or more rows above are gross/n-a"
    return f"<pre>{table}</pre>\n{total_line}"


def _intraday_section(kite, today_str: str) -> str:
    try:
        day = kite.positions().get("day", [])
    except Exception as exc:  # noqa: BLE001
        return f"(couldn't fetch intraday positions: {exc})"
    mis = [p for p in day if p.get("product") == "MIS" and p.get("quantity", 0) != 0]
    if not mis:
        return "No open intraday (MIS) positions right now."

    armed = _intraday_armed_symbols(today_str)
    symbols = [p["tradingsymbol"] for p in mis]
    try:
        quotes = kite.quote([f"NSE:{s}" for s in symbols])
    except Exception:
        quotes = {}

    rows = []
    total_gross = 0.0
    for p in mis:
        sym = p["tradingsymbol"]
        qty, avg = p["quantity"], p["average_price"]
        direction = "long" if qty > 0 else "short"
        arm_tag = " *" if sym in armed else ""
        q = quotes.get(f"NSE:{sym}")
        ltp = q.get("last_price") if q else None
        if ltp is None:
            rows.append((sym, _money(avg), "n/a", "n/a" + arm_tag))
            continue
        gross = (ltp - avg) * qty  # qty already carries the sign for a short
        total_gross += gross
        rows.append((sym, _money(avg), _money(ltp), _money(gross) + " (gross)" + arm_tag))

    table = _render_table(rows)
    return f"<pre>{table}</pre>\nIntraday TOTAL (gross): ₹{_money(total_gross)}"


def _sold_today_section(today_str: str) -> str:
    records = _todays_log_records("swing_*.jsonl", today_str) + _todays_log_records("intraday_manual_*.jsonl", today_str)
    sells = [r for r in records if r.get("kind") in ("swing_exit", "intraday_exit", "intraday_square_off")]
    if not sells:
        return "No sells today."
    sells.sort(key=lambda r: r.get("timestamp", ""))

    lines = []
    for r in sells:
        sym = r.get("symbol", "?")
        qty = r.get("qty")
        price = r.get("price")
        avg = r.get("avg_price")
        ts = r.get("timestamp", "")[11:16] or "--:--"
        if r["kind"] == "swing_exit":
            reason = "trailing stop"
            pnl = r.get("gross_pnl")
            pnl_label = "gross"
        else:
            reason = "15:08 square-off" if r["kind"] == "intraday_square_off" else "trailing stop"
            direction = r.get("direction", "long")
            pnl = (price - avg) * qty * (1 if direction == "long" else -1) if None not in (price, avg, qty) else None
            pnl_label = "gross"
        pnl_str = f"₹{_money(pnl)} ({pnl_label})" if pnl is not None else "n/a"
        lines.append(f"  {ts}  {sym} — {qty} @ ₹{_money(price)} ({reason}) → P&L {pnl_str}")
    return _escape_html("\n".join(lines))


def build_eod_table(kite) -> str | None:
    """Returns the HTML-ready message, or None if there is genuinely nothing
    to report (no open swing holdings, no open intraday positions, and no
    sells today) -- no message is sent that day in that case."""
    today = datetime.now(IST).date()
    today_str = today.isoformat()

    try:
        has_swing = bool([p for p in kite.positions().get("net", []) if p.get("product") == orders.PRODUCT_MTF and p.get("quantity", 0) > 0])
        has_intraday = bool([p for p in kite.positions().get("day", []) if p.get("product") == "MIS" and p.get("quantity", 0) != 0])
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ End-of-day report failed -- couldn't fetch positions: {exc}"
    sold_text = _sold_today_section(today_str)
    if not has_swing and not has_intraday and sold_text == "No sells today.":
        return None

    swing_text = _swing_section(kite, today)
    intraday_text = _intraday_section(kite, today_str)

    return (f"📋 End-of-day report — {today_str}\n\n"
            f"<b>Swing (MTF) holdings:</b>\n{swing_text}\n\n"
            f"<b>Intraday (MIS) positions:</b>\n{intraday_text}\n\n"
            f"<b>Sold today:</b>\n{sold_text}\n\n"
            f"* = trailing already armed")


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

    _log(f"digest thread started -- will fire at {EOD_TABLE_TIME} IST")
    sent_today: set[date] = set()
    last_heartbeat_hour = None
    while True:
        if should_stop is not None and should_stop():
            _log("digest thread stopping (should_stop)")
            return
        now = datetime.now(IST)
        # One line per hour just to prove the thread is still alive and looping --
        # cheap insurance against a silent hang that would otherwise look identical
        # to "nothing happened yet" until EOD_TABLE_TIME came and went with no table.
        if now.hour != last_heartbeat_hour:
            last_heartbeat_hour = now.hour
            _log(f"digest thread alive, next fire at {EOD_TABLE_TIME} IST (sent today: {bool(sent_today)})")
        if now.time() >= EOD_TABLE_TIME and now.date() not in sent_today:
            sent_today.add(now.date())
            try:
                kite = get_kite()
                message = build_eod_table(kite)
                if message and notifier:
                    sent_ok = notifier(message, parse_mode="HTML")
                    _log(f"end-of-day report sent (notifier returned {sent_ok!r})")
                elif message:
                    _log("end-of-day report built but no notifier configured -- not sent")
                else:
                    _log("end-of-day report skipped -- nothing open and no sells today")
            except Exception as exc:  # noqa: BLE001 - one bad day must never stop future ones
                _log(f"WARNING: end-of-day report failed: {exc!r}")
        time_module.sleep(poll_interval)
