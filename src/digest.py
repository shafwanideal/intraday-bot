"""End-of-day phone table, sent once at EOD_TABLE_TIME: a recap of every
MTF and MIS position actually CLOSED (sold) today, with P&L in both
rupees and % return, and a grand total.

Simplified 2026-11-11, explicit request: no longer shows open positions at
all (that's now the hourly update's job -- see hourly_pnl.py's
_open_positions_section, which also shows trailing/guaranteed-minimum-
profit status). This report now only ever has something to say on a day
with at least one sell; a day with zero sells sends nothing.

Replaces an earlier twice-daily (10:00/13:00) gross-P&L digest that turned
out to be clutter -- the explicit ask now is exactly one table, at 4:01 PM.
The three INSTANT alerts (trailing armed, sold, +-3% move) are unaffected --
they still fire from swing_live.py/intraday_live.py the moment they happen;
this module only covers the one daily report.

The BOT-TRIGGERED sold-today recap is reconstructed by replaying today's
own JSONL log files rather than depending on either guardian's live
in-memory state -- a guardian crash/restart can't break this report, and it
can't break a guardian either (every failure here is caught, never raised).

Extended 2026-11-15, explicit request, after a real gap surfaced: a
position closed MANUALLY (via the Kite app/website directly, not by the
guardian) never appears in that log-replay recap at all, because the
guardian only ever logs "...position_closed_externally" with no P&L for
one of those -- it never saw the fill. Two real trades (LUPIN, ONGC) closed
manually and were correctly shown in the hourly update (which already
FIFO-matches kite.orders() for exactly this reason -- see hourly_pnl.py's
own docstring) but silently missing from THIS report. Fixed by adding a
separate "Also closed today (booked manually)" section that reuses
hourly_pnl.compute_closed_today() and shows whatever it found that the
bot-triggered section above didn't already cover. This is the one thing in
this module that needs a live Kite session -- build_eod_table now takes
`kite` again.
"""

import json
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from . import hourly_pnl

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


def _signed_money(x) -> str:
    sign = "+" if x >= 0 else "-"
    return f"{sign}₹{abs(x):,.2f}"


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


def _sold_today_section(today_str: str) -> tuple[str, float, set[tuple[str, str]]]:
    """Returns (formatted lines, total gross P&L across every BOT-triggered
    sell today, {(symbol, product) already covered here}) -- the keys set
    lets _manually_closed_section below know which closes NOT to repeat.
    Each row now also shows the % RETURN (gross P&L / position value),
    added 2026-11-11 alongside the rupee figure, for both MTF and MIS."""
    records = _todays_log_records("swing_*.jsonl", today_str) + _todays_log_records("intraday_manual_*.jsonl", today_str)
    sells = [r for r in records if r.get("kind") in (
        "swing_exit", "swing_crash_stop_loss_exit", "intraday_exit", "intraday_stop_loss_exit", "intraday_square_off"
    )]
    if not sells:
        return "No sells today.", 0.0, set()
    sells.sort(key=lambda r: r.get("timestamp", ""))

    lines = []
    total = 0.0
    covered_keys: set[tuple[str, str]] = set()
    for r in sells:
        sym = r.get("symbol", "?")
        qty = r.get("qty")
        price = r.get("price")
        avg = r.get("avg_price")
        ts = r.get("timestamp", "")[11:16] or "--:--"
        if r["kind"] in ("swing_exit", "swing_crash_stop_loss_exit"):
            product = "MTF"
            reason = "CRASH STOP-LOSS" if r["kind"] == "swing_crash_stop_loss_exit" else "trailing stop"
            pnl = r.get("gross_pnl")
        else:
            product = "MIS"
            if r["kind"] == "intraday_square_off":
                reason = "15:08 square-off"
            elif r["kind"] == "intraday_stop_loss_exit":
                reason = "STOP-LOSS"
            else:
                reason = "trailing stop"
            direction = r.get("direction", "long")
            pnl = (price - avg) * qty * (1 if direction == "long" else -1) if None not in (price, avg, qty) else None
        if pnl is not None:
            total += pnl
        if pnl is not None and avg and qty:
            pct = pnl / (avg * qty) * 100
            pnl_str = f"₹{_money(pnl)} ({pct:+.2f}%, gross)"
        elif pnl is not None:
            pnl_str = f"₹{_money(pnl)} (gross)"
        else:
            pnl_str = "n/a"
        lines.append(f"  {ts}  {sym} ({product}) — {qty} @ ₹{_money(price)} ({reason}) → P&L {pnl_str}")
        covered_keys.add((sym, product))
    return _escape_html("\n".join(lines)), total, covered_keys


def _manually_closed_section(kite, covered_keys: set[tuple[str, str]]) -> tuple[str, float] | None:
    """Everything closed today per kite.orders() FIFO-matching (see
    hourly_pnl.compute_closed_today -- same technique that module already
    uses, which catches a sell booked MANUALLY via Kite directly, not just
    the guardian's own exits) whose (symbol, product) ISN'T already in
    covered_keys above. Returns None on an orders-fetch failure or when
    every closed symbol today was already bot-triggered (nothing left to
    add here)."""
    rows = hourly_pnl.compute_closed_today(kite)
    if rows is None:
        return None
    manual_rows = [r for r in rows if (r[0], r[1]) not in covered_keys]
    if not manual_rows:
        return None
    lines = []
    total = 0.0
    for symbol, product, qty, pnl, cost_basis in sorted(manual_rows, key=lambda r: -r[3]):
        total += pnl
        pct_str = f" ({pnl / cost_basis * 100:+.2f}%)" if cost_basis else ""
        lines.append(f"  {symbol} ({product}) — qty {qty}: {_signed_money(pnl)}{pct_str}")
    return _escape_html("\n".join(lines)), total


def build_eod_table(kite) -> str | None:
    """Returns the HTML-ready message, or None if nothing closed today at
    all -- bot-triggered or manual (no message is sent that day in that
    case; this report no longer shows open positions, see this module's own
    docstring, so a quiet day genuinely has nothing to say here)."""
    today_str = datetime.now(IST).date().isoformat()
    sold_text, sold_total, covered_keys = _sold_today_section(today_str)
    manual = _manually_closed_section(kite, covered_keys)

    if sold_text == "No sells today." and manual is None:
        return None

    grand_total = sold_total + (manual[1] if manual else 0.0)
    total_line = f"<b>Total realized P&amp;L today (gross, all sells): {_signed_money(grand_total)}</b>\n\n"

    bot_block = f"<b>Closed today (MTF &amp; MIS):</b>\n{sold_text}\n\n" if sold_text != "No sells today." else ""
    manual_block = f"<b>Also closed today (booked manually, not by the bot):</b>\n{manual[0]}\n\n" if manual else ""

    return f"📋 End-of-day report — {today_str}\n\n{bot_block}{manual_block}{total_line}"


def run_digest_loop(get_kite, notifier, should_stop=None, poll_interval: int = 30) -> None:
    """Sends build_eod_table() once, at EOD_TABLE_TIME, once per calendar
    day. Meant to run as its own daemon thread alongside the guardians (see
    scripts/run_all_guardians.py) -- a failure here is logged, never raised,
    so it can't take a guardian down, and a guardian crash/restart can't
    break this loop either.

    `get_kite` is used again as of 2026-11-15 -- build_eod_table() needs a
    live session for its manually-closed section (hourly_pnl.compute_closed_
    today calls kite.orders()). If that call fails (e.g. a stale token), the
    bot-triggered section above still gets built and sent on its own --
    _manually_closed_section returns None on a fetch failure rather than
    raising, so a Kite hiccup degrades this report, it doesn't blank it.

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
            _log(f"digest thread alive, next fire at {EOD_TABLE_TIME} IST (sent today: {now.date() in sent_today})")
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
                    _log("end-of-day report skipped -- no sells today")
            except Exception as exc:  # noqa: BLE001 - one bad day must never stop future ones
                _log(f"WARNING: end-of-day report failed: {exc!r}")
        time_module.sleep(poll_interval)
