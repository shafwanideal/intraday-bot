"""Live market-depth imbalance scanner, with price + volume confirmation.

Screens a universe of NSE symbols for stocks where the visible top-5-level
order book is heavily skewed toward buyers (or sellers) -- the same "91%
buyers, 8% sellers" read that preceded GRANULESIND's two intraday up-legs
on 2026-09-11 (13:05 IST and 13:55 IST) -- and sends a Telegram alert only
when that depth read is corroborated by the tape: price already at/near a
fresh intraday extreme, and a burst in trading pace relative to the
stock's own average pace so far today. Depth alone is noisy (the top-5
book can be pulled or refreshed within seconds); GRANULESIND's own move
only confirmed the read because both legs came with a real volume spike
and price already breaking the recent range at the same time.

Read-only: this only calls kite.quote(), never places or suggests an order.
"""

import time as time_module
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

import requests
from kiteconnect.exceptions import KiteException

from . import auth, telegram_notify
from .screener import load_nifty200_symbols

IST = ZoneInfo("Asia/Kolkata")

# Anything network-shaped as well as Kite's own error responses -- a single
# failed scan must never crash the whole polling loop (same reasoning as
# shadow.py's POLL_EXCEPTIONS).
POLL_EXCEPTIONS = (KiteException, requests.exceptions.RequestException)

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
# kite.quote() is rate-limited to ~1 request/second on Kite Connect, and one
# call covers the whole symbol universe (up to 500 instruments), so this only
# needs to stay above ~1-2s -- 10s leaves comfortable headroom.
POLL_INTERVAL_SECONDS = 10
CLOSED_MARKET_SLEEP_SECONDS = 300

BUY_IMBALANCE_THRESHOLD = 0.85  # fraction of visible depth qty on the buy side to trigger
SELL_IMBALANCE_THRESHOLD = 0.85  # same, sell side
# Ignore symbols with a thin top-5 book -- a 95/5 split on a couple hundred
# shares total is noise from a low-liquidity stock, not the kind of
# institutional-size imbalance that moved GRANULESIND.
MIN_TOTAL_DEPTH_QTY = 5_000
# Don't re-alert the same symbol+side on every single poll while it sits
# past threshold -- re-notify at most this often.
RENOTIFY_COOLDOWN_MINUTES = 15

# --- Confirmation filter ---------------------------------------------------
# "Within this band of today's high/low" counts as currently making (or
# right at) a fresh intraday extreme, i.e. actually breaking out -- not just
# up or down on the day. 0.15% comfortably covers the last-traded-price lag
# behind Kite's own running day-high/day-low without being so loose it
# matches a stock that peaked an hour ago and has since drifted off.
BREAKOUT_BAND_PCT = 0.0015
# Recent trading pace must be at least this many times the stock's own
# average pace since today's open to count as a volume spike. Compared
# against the stock's own day-so-far average rather than a stored historical
# average, so it needs no extra data and self-normalizes per stock.
VOLUME_SPIKE_MULTIPLIER = 3.0
# Skip volume-pace confirmation for this long after open -- the day's
# average pace is too noisy to mean anything in the first few minutes
# (opening-auction volume dominates it).
MIN_ELAPSED_SECONDS_FOR_VOLUME_CHECK = 300


def _now() -> datetime:
    return datetime.now(IST)


@dataclass
class DepthSignal:
    symbol: str
    side: str  # "buy" or "sell"
    buy_qty: int
    sell_qty: int
    imbalance_pct: float  # fraction of total visible qty on the triggering side
    ltp: float
    volume_ratio: float | None = None  # set once confirm_signals() has checked it


def compute_depth_qty(quote: dict) -> tuple[int, int] | None:
    """Total resting quantity across the visible 5 bid and 5 ask levels.
    Returns None if this quote has no depth data at all."""
    depth = quote.get("depth")
    if not depth:
        return None
    buy_qty = sum(level["quantity"] for level in depth.get("buy", []))
    sell_qty = sum(level["quantity"] for level in depth.get("sell", []))
    return buy_qty, sell_qty


def fetch_quotes(kite, symbols: list[str]) -> dict:
    instruments = [f"NSE:{sym}" for sym in symbols]
    return kite.quote(instruments)


def depth_candidates(quotes: dict, symbols: list[str]) -> list[DepthSignal]:
    """Pure function: which symbols currently have a top-5 book skewed past
    threshold on either side, most imbalanced first. No price/volume
    confirmation yet -- see confirm_signals() for that."""
    signals = []
    for symbol in symbols:
        quote = quotes.get(f"NSE:{symbol}")
        if quote is None:
            continue
        qtys = compute_depth_qty(quote)
        if qtys is None:
            continue
        buy_qty, sell_qty = qtys
        total = buy_qty + sell_qty
        if total < MIN_TOTAL_DEPTH_QTY:
            continue

        buy_pct = buy_qty / total
        if buy_pct >= BUY_IMBALANCE_THRESHOLD:
            signals.append(DepthSignal(symbol, "buy", buy_qty, sell_qty, buy_pct, quote["last_price"]))
        elif (1 - buy_pct) >= SELL_IMBALANCE_THRESHOLD:
            signals.append(DepthSignal(symbol, "sell", buy_qty, sell_qty, 1 - buy_pct, quote["last_price"]))

    signals.sort(key=lambda s: s.imbalance_pct, reverse=True)
    return signals


def _price_confirms(quote: dict, side: str) -> bool:
    """True if last_price is at/very near today's high (buy side) or low
    (sell side) -- i.e. actually breaking out right now, the same thing
    that was true going into both GRANULESIND legs on 2026-09-11."""
    ohlc = quote.get("ohlc") or {}
    last_price = quote.get("last_price")
    if not last_price:
        return False
    if side == "buy":
        day_high = ohlc.get("high")
        return bool(day_high) and last_price >= day_high * (1 - BREAKOUT_BAND_PCT)
    day_low = ohlc.get("low")
    return bool(day_low) and last_price <= day_low * (1 + BREAKOUT_BAND_PCT)


def _volume_ratio(
    symbol: str,
    quote: dict,
    now: datetime,
    market_open_dt: datetime,
    volume_state: dict[str, tuple[datetime, int]],
) -> float | None:
    """Recent trading pace (since the last poll) as a multiple of the
    stock's own average pace since today's open. Always updates
    volume_state for `symbol` as a side effect; returns None when there's
    no prior sample to compare against yet (first sighting of the day) or
    it's too early in the session for the average pace to mean anything.
    """
    current_volume = quote.get("volume") or 0
    prev = volume_state.get(symbol)
    volume_state[symbol] = (now, current_volume)
    if prev is None:
        return None

    elapsed_since_open = (now - market_open_dt).total_seconds()
    if elapsed_since_open < MIN_ELAPSED_SECONDS_FOR_VOLUME_CHECK:
        return None

    prev_time, prev_volume = prev
    poll_elapsed = (now - prev_time).total_seconds()
    if poll_elapsed <= 0:
        return None

    baseline_rate = current_volume / elapsed_since_open
    if baseline_rate <= 0:
        return None
    recent_rate = max(current_volume - prev_volume, 0) / poll_elapsed
    return recent_rate / baseline_rate


def confirm_signals(
    signals: list[DepthSignal],
    quotes: dict,
    now: datetime,
    market_open_dt: datetime,
    volume_state: dict[str, tuple[datetime, int]],
) -> list[DepthSignal]:
    """Filters `signals` down to the ones where price action and trading
    pace agree with the depth read. Calls _volume_ratio() for every signal
    (not just ones that pass the price check) so volume_state stays warm
    for every symbol currently showing a depth skew, not just confirmed
    ones -- otherwise a symbol that only clears the price check on its
    second poll would still be missing its first volume sample."""
    confirmed = []
    for signal in signals:
        quote = quotes.get(f"NSE:{signal.symbol}")
        if quote is None:
            continue
        ratio = _volume_ratio(signal.symbol, quote, now, market_open_dt, volume_state)
        if not _price_confirms(quote, signal.side):
            continue
        if ratio is None or ratio < VOLUME_SPIKE_MULTIPLIER:
            continue
        signal.volume_ratio = ratio
        confirmed.append(signal)
    return confirmed


def _format_alert(signal: DepthSignal) -> str:
    side_word = "BUYERS" if signal.side == "buy" else "SELLERS"
    extreme_word = "day's high" if signal.side == "buy" else "day's low"
    return (
        f"CONFIRMED DEPTH ALERT: {signal.symbol}\n"
        f"{signal.imbalance_pct:.0%} {side_word} in top-5 book "
        f"(buy {signal.buy_qty:,} / sell {signal.sell_qty:,})\n"
        f"Price at/near {extreme_word}, volume pace {signal.volume_ratio:.1f}x today's average\n"
        f"LTP: Rs {signal.ltp:,.2f}\n"
        f"Depth + price + volume all agree -- still your call, not an auto-entry."
    )


def run_depth_scanner(symbols: list[str] | None = None, poll_interval: int = POLL_INTERVAL_SECONDS) -> None:
    """Poll `symbols` (default: Nifty 200) during market hours and send a
    Telegram alert the first time a symbol's depth imbalance is confirmed
    by price action + volume pace, then at most once every
    RENOTIFY_COOLDOWN_MINUTES while it stays confirmed. Read-only -- never
    places an order."""
    symbols = symbols or load_nifty200_symbols()
    kite = auth.get_kite()

    if not telegram_notify.enabled():
        print("WARNING: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set -- alerts will only print to console.")

    last_notified: dict[tuple[str, str], datetime] = {}
    volume_state: dict[str, tuple[datetime, int]] = {}
    current_day = None

    print(f"Depth scanner starting -- watching {len(symbols)} symbols, poll every {poll_interval}s")
    print(
        f"Depth thresholds: buy >= {BUY_IMBALANCE_THRESHOLD:.0%}, sell >= {SELL_IMBALANCE_THRESHOLD:.0%}, "
        f"min book qty {MIN_TOTAL_DEPTH_QTY:,}"
    )
    print(
        f"Confirmation: price within {BREAKOUT_BAND_PCT:.2%} of day's high/low, "
        f"volume pace >= {VOLUME_SPIKE_MULTIPLIER:.1f}x today's average\n"
    )

    try:
        while True:
            now = _now()
            if now.date() != current_day:
                # New trading day -- yesterday's cumulative volume is meaningless
                # as a baseline for today, so start the pace comparison fresh.
                volume_state.clear()
                current_day = now.date()
            market_open_dt = datetime.combine(now.date(), MARKET_OPEN, tzinfo=IST)

            if now.time() < MARKET_OPEN or now.time() >= MARKET_CLOSE:
                print(f"[{now.isoformat()}] Market closed, waiting...")
                time_module.sleep(CLOSED_MARKET_SLEEP_SECONDS)
                continue

            try:
                quotes = fetch_quotes(kite, symbols)
            except POLL_EXCEPTIONS as exc:
                print(f"WARNING: scan failed: {exc}")
                time_module.sleep(poll_interval)
                continue

            candidates = depth_candidates(quotes, symbols)
            confirmed = confirm_signals(candidates, quotes, now, market_open_dt, volume_state)
            if candidates:
                print(
                    f"[{now.isoformat()}] {len(candidates)} depth candidate(s) "
                    f"({', '.join(s.symbol for s in candidates)}), {len(confirmed)} confirmed"
                )

            for signal in confirmed:
                key = (signal.symbol, signal.side)
                last = last_notified.get(key)
                if last and (now - last).total_seconds() < RENOTIFY_COOLDOWN_MINUTES * 60:
                    continue
                message = _format_alert(signal)
                print(f"[{now.isoformat()}] {message}\n")
                telegram_notify.send_message(message)
                last_notified[key] = now

            time_module.sleep(poll_interval)
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
