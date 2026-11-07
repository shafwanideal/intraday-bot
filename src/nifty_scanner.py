"""Nifty50 market-wide scanner: gap-at-open and intraday big-move alerts.

Explicit ask, two-tier (2026-11-08, collapsed from an earlier flat-3%-plus-
tiered-4%/6% design into exactly two non-overlapping tiers covering every
Nifty50 symbol): the 25 biggest/most liquid Nifty50 names by index weight
("top 25") alert at a tighter 4%, since a move that size in a mega-cap is
more unusual; the remaining 25 alert at a looser 5%. Rendered with a
green-alarm marker so it reads distinctly from the red crash-stop-loss
sell alert. Telegram's Bot API has no actual colored-text support (its
HTML parse mode is bold/italic/code/links only) -- "green colour alarm" is
a green emoji marker, not literally colored text, which isn't possible
over this transport.

A market-wide watchlist scan, independent of src/swing_live.py's
per-position tracking, which only ever looks at symbols actually held.

Notification only. This module never places an order -- it exists purely
so the trader can look at a mover and decide manually whether to act.

Reuses NIFTY_50_SYMBOLS and _fetch_quotes from swing_live.py rather than
keeping a second copy of either -- see swing_live.py's own staleness note
on that symbol set (periodically go reverify it against NSE's index page).
"""

import time as time_module
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from . import orders
from .swing_live import NIFTY_50_SYMBOLS, _fetch_quotes

IST = ZoneInfo("Asia/Kolkata")
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
SCANNER_LOG_FILE = LOG_DIR / "nifty_scanner.log"

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
GAP_CHECK_AFTER = time(9, 16)  # give the opening auction a minute to settle

# Best-effort "top 25 of Nifty50 by index weight" -- same staleness caveat as
# swing_live.NIFTY_50_SYMBOLS itself: NSE rebalances the index twice a year, and
# RELATIVE weights drift between rebalances too, so this ordering will go stale
# faster than membership does. Verify against NSE's published Nifty50 weightage
# sheet periodically. Every name here MUST also be in NIFTY_50_SYMBOLS (asserted
# below at import time) -- a typo here must fail loudly, not silently drop a
# symbol out of BOTH tiers.
NIFTY_TOP_25_SYMBOLS = {
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "BHARTIARTL", "LT", "ITC", "SBIN",
    "HINDUNILVR", "KOTAKBANK", "AXISBANK", "BAJFINANCE", "M&M", "MARUTI", "SUNPHARMA",
    "HCLTECH", "TITAN", "ULTRACEMCO", "ASIANPAINT", "BAJAJFINSV", "TATAMOTORS", "NTPC",
    "ONGC", "ADANIENT",
}
assert NIFTY_TOP_25_SYMBOLS <= NIFTY_50_SYMBOLS, "NIFTY_TOP_25_SYMBOLS has a symbol not in NIFTY_50_SYMBOLS"
assert len(NIFTY_TOP_25_SYMBOLS) == 25, len(NIFTY_TOP_25_SYMBOLS)
NIFTY_OTHER_25_SYMBOLS = NIFTY_50_SYMBOLS - NIFTY_TOP_25_SYMBOLS

TOP25_MOVE_PCT = 0.04    # 4%, the tighter tier
OTHER25_MOVE_PCT = 0.05  # 5%, the looser tier -- lowered from 6% on 2026-11-08

GREEN_ALARM = "\U0001f7e2\U0001f514"  # green circle + bell -- "green colour alarm" request


def _threshold_pct(symbol: str) -> float:
    return TOP25_MOVE_PCT if symbol in NIFTY_TOP_25_SYMBOLS else OTHER25_MOVE_PCT


def _tier_label(symbol: str) -> str:
    return "top25/4%" if symbol in NIFTY_TOP_25_SYMBOLS else "other25/5%"


def _log(line: str) -> None:
    stamped = f"{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')} {line}"
    print(stamped, flush=True)
    try:
        SCANNER_LOG_FILE.parent.mkdir(exist_ok=True)
        with open(SCANNER_LOG_FILE, "a") as f:
            f.write(stamped + "\n")
    except OSError:
        pass


def _fetch_quotes_with_open(kite, symbols: list[str]) -> dict[str, dict]:
    """Like swing_live._fetch_quotes but also carries today's open price,
    needed for the opening-gap check -- that function's existing callers
    (the crash stop-loss) never need open, so it's kept separate here
    rather than widening that function's contract for every caller."""
    if not symbols:
        return {}
    try:
        quotes = kite.quote([f"NSE:{s}" for s in symbols])
    except orders.PLACEMENT_EXCEPTIONS as exc:
        _log(f"WARNING: could not fetch quotes: {exc!r}")
        return {}
    result = {}
    for symbol in symbols:
        q = quotes.get(f"NSE:{symbol}")
        if q and q.get("last_price") is not None:
            ohlc = q.get("ohlc") or {}
            prev_close = ohlc.get("close")
            open_price = ohlc.get("open")
            result[symbol] = {
                "ltp": float(q["last_price"]),
                "prev_close": float(prev_close) if prev_close else None,
                "open": float(open_price) if open_price else None,
            }
    return result


def _signed_pct(pct: float) -> str:
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.2f}%"


def _move_pct(price: float, prev_close: float) -> float:
    return (price - prev_close) / prev_close * 100


def check_gap_at_open(kite) -> tuple[str | None, set[str]]:
    """One-shot check of every Nifty50 symbol's opening gap vs yesterday's
    close, against each symbol's own tier threshold (top25 4%, other25 5%).
    Returns (message_or_None, {symbols that gapped past their own
    threshold}) -- the caller marks those symbols as already-notified so
    the continuous check below doesn't re-alert on the same move."""
    quotes = _fetch_quotes_with_open(kite, sorted(NIFTY_50_SYMBOLS))
    movers = []
    for symbol, q in quotes.items():
        if not q["prev_close"] or not q["open"]:
            continue
        gap = _move_pct(q["open"], q["prev_close"])
        if abs(gap) >= _threshold_pct(symbol) * 100:
            movers.append((symbol, gap, q["prev_close"], q["open"]))
    if not movers:
        return None, set()
    movers.sort(key=lambda m: -abs(m[1]))
    lines = [f"{GREEN_ALARM} Nifty50 ALERT: top25 4%+ / other25 5%+ (at open):"]
    for symbol, gap, prev_close, open_price in movers:
        lines.append(f"{symbol} [{_tier_label(symbol)}]: {_signed_pct(gap)} (prev close ₹{prev_close:,.2f} → open ₹{open_price:,.2f})")
    return "\n".join(lines), {m[0] for m in movers}


def check_intraday_movers(kite, already_notified: set[str]) -> tuple[str | None, set[str]]:
    """Continuous check across the rest of the day: any Nifty50 symbol not
    already notified today that has now moved past its own tier threshold
    vs yesterday's close, even if it opened flat."""
    to_check = sorted(NIFTY_50_SYMBOLS - already_notified)
    if not to_check:
        return None, set()
    quotes = _fetch_quotes(kite, to_check)
    movers = []
    for symbol, q in quotes.items():
        prev_close = q.get("prev_close")
        if not prev_close:
            continue
        move = _move_pct(q["ltp"], prev_close)
        if abs(move) >= _threshold_pct(symbol) * 100:
            movers.append((symbol, move, prev_close, q["ltp"]))
    if not movers:
        return None, set()
    movers.sort(key=lambda m: -abs(m[1]))
    lines = [f"{GREEN_ALARM} Nifty50 ALERT: top25 4%+ / other25 5%+:"]
    for symbol, move, prev_close, ltp in movers:
        lines.append(f"{symbol} [{_tier_label(symbol)}]: {_signed_pct(move)} (prev close ₹{prev_close:,.2f} → now ₹{ltp:,.2f})")
    return "\n".join(lines), {m[0] for m in movers}


def run_nifty_scanner_loop(get_kite, notifier, should_stop=None, poll_interval: int = 60) -> None:
    """Market-wide Nifty50 scanner -- notification only, places no orders.
    Fires the opening-gap message once per day, then keeps watching every
    poll for any OTHER Nifty50 symbol crossing ITS tier's threshold later
    in the session (top25 at 4%, the other 25 at 5%), alerting the first
    time each symbol crosses (never repeats for the same symbol same day)."""
    _log("Nifty50 scanner thread started")
    today = None
    gap_done = False
    notified_today: set[str] = set()
    while True:
        if should_stop is not None and should_stop():
            _log("Nifty50 scanner thread stopping (should_stop)")
            return
        now = datetime.now(IST)
        if today != now.date():
            today, gap_done, notified_today = now.date(), False, set()

        if MARKET_OPEN <= now.time() <= MARKET_CLOSE:
            try:
                kite = get_kite()
                if not gap_done and now.time() >= GAP_CHECK_AFTER:
                    gap_done = True
                    message, gapped = check_gap_at_open(kite)
                    notified_today |= gapped
                    if message and notifier:
                        ok = notifier(message)
                        _log(f"gap-at-open alert {'sent' if ok else 'FAILED to send'} ({len(gapped)} symbols)")
                    elif message:
                        _log(f"gap-at-open alert built but no notifier configured ({len(gapped)} symbols)")
                    else:
                        _log("gap-at-open check: nothing past its threshold today")
                elif gap_done:
                    message, movers = check_intraday_movers(kite, notified_today)
                    notified_today |= movers
                    if message and notifier:
                        ok = notifier(message)
                        _log(f"intraday mover alert {'sent' if ok else 'FAILED to send'} ({len(movers)} symbols)")
            except Exception as exc:  # noqa: BLE001 - one bad poll must never kill this thread
                _log(f"poll failed: {exc!r}")
        time_module.sleep(poll_interval)
