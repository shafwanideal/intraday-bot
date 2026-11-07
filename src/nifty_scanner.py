"""Nifty50 market-wide scanner: gap-at-open and intraday big-move alerts.

Explicit ask #1: spot ANY Nifty50 stock (not just ones the trader holds)
that gaps open, or later moves intraday, 3% or more either way vs
yesterday's close -- a market-wide watchlist scan, independent of
src/swing_live.py's per-position tracking, which only ever looks at
symbols actually held.

Explicit ask #2 (2026-11-07, kept running ALONGSIDE #1, not replacing it):
a second, louder tier -- the 25 biggest/most liquid Nifty50 names by index
weight ("top 25") alert at a tighter 4%, since a move that size in a
mega-cap is more unusual; the remaining 25 (smaller within Nifty50) alert
at a looser 6%. Rendered with a green-alarm marker so it reads distinctly
from both the plain #1 alert and the red crash-stop-loss sell alert.
Telegram's Bot API has no actual colored-text support (its HTML parse
mode is bold/italic/code/links only) -- "green colour alarm" is a green
emoji marker, not literally colored text.

Notification only, both tiers. This module never places an order -- it
exists purely so the trader can look at a mover and decide manually
whether to act.

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
MOVE_PCT = 0.03  # 3%, either direction, vs yesterday's close -- the flat, all-50 tier

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

TOP25_MOVE_PCT = 0.04   # 4%, the tighter tier
OTHER25_MOVE_PCT = 0.06  # 6%, the looser tier

GREEN_ALARM = "\U0001f7e2\U0001f514"  # green circle + bell -- "green colour alarm" request


def _tiered_threshold_pct(symbol: str) -> float:
    return TOP25_MOVE_PCT if symbol in NIFTY_TOP_25_SYMBOLS else OTHER25_MOVE_PCT


def _tier_label(symbol: str) -> str:
    return "top25/4%" if symbol in NIFTY_TOP_25_SYMBOLS else "other25/6%"


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


def _scan_gap_at_open(kite, threshold_for, header: str, tiered: bool) -> tuple[str | None, set[str]]:
    """One-shot check of every Nifty50 symbol's opening gap vs yesterday's
    close, against a PER-SYMBOL threshold (threshold_for(symbol) -> float
    fraction, e.g. 0.03). Returns (message_or_None, {symbols that gapped
    past their own threshold}) -- the caller marks those symbols as
    already-notified for THIS tier so the continuous check below doesn't
    re-alert on the same move."""
    quotes = _fetch_quotes_with_open(kite, sorted(NIFTY_50_SYMBOLS))
    movers = []
    for symbol, q in quotes.items():
        if not q["prev_close"] or not q["open"]:
            continue
        gap = _move_pct(q["open"], q["prev_close"])
        if abs(gap) >= threshold_for(symbol) * 100:
            movers.append((symbol, gap, q["prev_close"], q["open"]))
    if not movers:
        return None, set()
    movers.sort(key=lambda m: -abs(m[1]))
    lines = [f"{header} (at open):"]
    for symbol, gap, prev_close, open_price in movers:
        tier = f" [{_tier_label(symbol)}]" if tiered else ""
        lines.append(f"{symbol}{tier}: {_signed_pct(gap)} (prev close ₹{prev_close:,.2f} → open ₹{open_price:,.2f})")
    return "\n".join(lines), {m[0] for m in movers}


def _scan_intraday_movers(kite, already_notified: set[str], threshold_for, header: str, tiered: bool) -> tuple[str | None, set[str]]:
    """Continuous check across the rest of the day: any Nifty50 symbol not
    already notified today (for THIS tier) that has now moved past its own
    threshold vs yesterday's close, even if it opened flat."""
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
        if abs(move) >= threshold_for(symbol) * 100:
            movers.append((symbol, move, prev_close, q["ltp"]))
    if not movers:
        return None, set()
    movers.sort(key=lambda m: -abs(m[1]))
    lines = [f"{header}:"]
    for symbol, move, prev_close, ltp in movers:
        tier = f" [{_tier_label(symbol)}]" if tiered else ""
        lines.append(f"{symbol}{tier}: {_signed_pct(move)} (prev close ₹{prev_close:,.2f} → now ₹{ltp:,.2f})")
    return "\n".join(lines), {m[0] for m in movers}


def check_gap_at_open(kite) -> tuple[str | None, set[str]]:
    """Flat 3%-for-all-50 tier."""
    return _scan_gap_at_open(kite, lambda s: MOVE_PCT, "⚡ Nifty50 gap movers (3%+ vs yesterday's close)", tiered=False)


def check_intraday_movers(kite, already_notified: set[str]) -> tuple[str | None, set[str]]:
    """Flat 3%-for-all-50 tier."""
    return _scan_intraday_movers(
        kite, already_notified, lambda s: MOVE_PCT, "\U0001f4ca Nifty50 big move (3%+ vs yesterday's close)", tiered=False
    )


def check_gap_at_open_tiered(kite) -> tuple[str | None, set[str]]:
    """Louder tier: top25 Nifty50 names by weight at 4%, the other 25 at 6%."""
    return _scan_gap_at_open(kite, _tiered_threshold_pct, f"{GREEN_ALARM} Nifty50 ALERT: top25 4%+ / other25 6%+", tiered=True)


def check_intraday_movers_tiered(kite, already_notified: set[str]) -> tuple[str | None, set[str]]:
    """Louder tier: top25 Nifty50 names by weight at 4%, the other 25 at 6%."""
    return _scan_intraday_movers(
        kite, already_notified, _tiered_threshold_pct, f"{GREEN_ALARM} Nifty50 ALERT: top25 4%+ / other25 6%+", tiered=True
    )


def run_nifty_scanner_loop(get_kite, notifier, should_stop=None, poll_interval: int = 60) -> None:
    """Market-wide Nifty50 scanner -- notification only, places no orders.
    Runs TWO independent alert tiers off the same poll, each with its own
    once-a-day gap check plus continuous intraday watch, and its own
    per-symbol already-notified tracking (a symbol can fire in one tier
    without that affecting the other):

    - flat tier: any Nifty50 symbol, 3% vs yesterday's close.
    - tiered/green-alarm tier: the top 25 by weight at 4%, the other 25 at
      6%, rendered with a distinct green-alarm marker.
    """
    _log("Nifty50 scanner thread started")
    today = None
    flat_gap_done = tiered_gap_done = False
    flat_notified: set[str] = set()
    tiered_notified: set[str] = set()
    while True:
        if should_stop is not None and should_stop():
            _log("Nifty50 scanner thread stopping (should_stop)")
            return
        now = datetime.now(IST)
        if today != now.date():
            today = now.date()
            flat_gap_done = tiered_gap_done = False
            flat_notified = set()
            tiered_notified = set()

        if MARKET_OPEN <= now.time() <= MARKET_CLOSE:
            try:
                kite = get_kite()

                if not flat_gap_done and now.time() >= GAP_CHECK_AFTER:
                    flat_gap_done = True
                    message, gapped = check_gap_at_open(kite)
                    flat_notified |= gapped
                    if message and notifier:
                        ok = notifier(message)
                        _log(f"[flat] gap-at-open alert {'sent' if ok else 'FAILED to send'} ({len(gapped)} symbols)")
                    elif message:
                        _log(f"[flat] gap-at-open alert built but no notifier configured ({len(gapped)} symbols)")
                    else:
                        _log("[flat] gap-at-open check: nothing gapped 3%+ today")
                elif flat_gap_done:
                    message, movers = check_intraday_movers(kite, flat_notified)
                    flat_notified |= movers
                    if message and notifier:
                        ok = notifier(message)
                        _log(f"[flat] intraday mover alert {'sent' if ok else 'FAILED to send'} ({len(movers)} symbols)")

                if not tiered_gap_done and now.time() >= GAP_CHECK_AFTER:
                    tiered_gap_done = True
                    message, gapped = check_gap_at_open_tiered(kite)
                    tiered_notified |= gapped
                    if message and notifier:
                        ok = notifier(message)
                        _log(f"[tiered] gap-at-open alert {'sent' if ok else 'FAILED to send'} ({len(gapped)} symbols)")
                    elif message:
                        _log(f"[tiered] gap-at-open alert built but no notifier configured ({len(gapped)} symbols)")
                    else:
                        _log("[tiered] gap-at-open check: nothing past its threshold today")
                elif tiered_gap_done:
                    message, movers = check_intraday_movers_tiered(kite, tiered_notified)
                    tiered_notified |= movers
                    if message and notifier:
                        ok = notifier(message)
                        _log(f"[tiered] intraday mover alert {'sent' if ok else 'FAILED to send'} ({len(movers)} symbols)")
            except Exception as exc:  # noqa: BLE001 - one bad poll must never kill this thread
                _log(f"poll failed: {exc!r}")
        time_module.sleep(poll_interval)
