"""Live market-depth imbalance scanner.

Screens a universe of NSE symbols for stocks where the visible top-5-level
order book is heavily skewed toward buyers (or sellers) and sends a
Telegram alert when the skew crosses a threshold -- the same "91% buyers,
8% sellers" read that preceded GRANULESIND's two intraday up-legs on
2026-09-11 (13:05 IST and 13:55 IST), now automated instead of eyeballed.

Read-only: this only calls kite.quote(), never places or suggests an
order. Depth alone is a noisy signal -- the top-5 book can be pulled or
refreshed within seconds (spoofing, or a market maker just repricing), so
GRANULESIND's own move only confirmed the read because it came with a real
volume spike and price already breaking the recent range at the same
time. Treat every alert here as "go look at the chart," not as an entry.
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


def compute_depth_qty(quote: dict) -> tuple[int, int] | None:
    """Total resting quantity across the visible 5 bid and 5 ask levels.
    Returns None if this quote has no depth data at all."""
    depth = quote.get("depth")
    if not depth:
        return None
    buy_qty = sum(level["quantity"] for level in depth.get("buy", []))
    sell_qty = sum(level["quantity"] for level in depth.get("sell", []))
    return buy_qty, sell_qty


def scan_once(kite, symbols: list[str]) -> list[DepthSignal]:
    """One pass over `symbols`: fetch live quotes+depth and return every
    symbol currently past the imbalance threshold on either side, most
    imbalanced first."""
    instruments = [f"NSE:{sym}" for sym in symbols]
    quotes = kite.quote(instruments)

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


def _format_alert(signal: DepthSignal) -> str:
    side_word = "BUYERS" if signal.side == "buy" else "SELLERS"
    return (
        f"DEPTH ALERT: {signal.symbol}\n"
        f"{signal.imbalance_pct:.0%} {side_word} in top-5 book "
        f"(buy {signal.buy_qty:,} / sell {signal.sell_qty:,})\n"
        f"LTP: Rs {signal.ltp:,.2f}\n"
        f"Confirm with price action + volume before entering -- depth alone can flip in seconds."
    )


def run_depth_scanner(symbols: list[str] | None = None, poll_interval: int = POLL_INTERVAL_SECONDS) -> None:
    """Poll `symbols` (default: Nifty 200) during market hours and send a
    Telegram alert the first time a symbol crosses the buy/sell depth
    imbalance threshold, then at most once every RENOTIFY_COOLDOWN_MINUTES
    while it stays past threshold. Read-only -- never places an order."""
    symbols = symbols or load_nifty200_symbols()
    kite = auth.get_kite()

    if not telegram_notify.enabled():
        print("WARNING: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set -- alerts will only print to console.")

    last_notified: dict[tuple[str, str], datetime] = {}

    print(f"Depth scanner starting -- watching {len(symbols)} symbols, poll every {poll_interval}s")
    print(
        f"Thresholds: buy >= {BUY_IMBALANCE_THRESHOLD:.0%}, sell >= {SELL_IMBALANCE_THRESHOLD:.0%}, "
        f"min book qty {MIN_TOTAL_DEPTH_QTY:,}\n"
    )

    try:
        while True:
            now = _now()
            if now.time() < MARKET_OPEN or now.time() >= MARKET_CLOSE:
                print(f"[{now.isoformat()}] Market closed, waiting...")
                time_module.sleep(CLOSED_MARKET_SLEEP_SECONDS)
                continue

            try:
                signals = scan_once(kite, symbols)
            except POLL_EXCEPTIONS as exc:
                print(f"WARNING: scan failed: {exc}")
                time_module.sleep(poll_interval)
                continue

            for signal in signals:
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
