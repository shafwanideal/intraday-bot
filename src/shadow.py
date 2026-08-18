import json
import time as time_module
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from kiteconnect.exceptions import KiteException

from . import auth, kite_data

# Anything network-shaped (timeout, connection reset, DNS blip) as well as
# Kite's own error responses -- a poll failing must never crash the whole
# session. Over a 6-hour polling loop, a transient network hiccup is not a
# rare edge case, it's close to guaranteed to happen at least once.
POLL_EXCEPTIONS = (KiteException, requests.exceptions.RequestException)
from .strategy import DEFAULT_ATR_MULTIPLIER, SQUARE_OFF_TIME, GridEngine

# NSE trades on IST wall-clock time regardless of what timezone the machine
# running this script is set to (e.g. a VPS defaulting to UTC or the host's
# own timezone). Always compute "now" explicitly in IST rather than trusting
# datetime.now() to already be IST -- a bug this exact mismatch caused once
# (server was in Europe/Berlin, so a naive "now" fell inside market hours
# when the real IST time was already past close).
IST = ZoneInfo("Asia/Kolkata")


def _now() -> datetime:
    return datetime.now(IST)


MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
POLL_INTERVAL_SECONDS = 15
LATE_ENTRY_CUTOFF = time(14, 30)  # don't take new entries this close to square-off

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TODAYS_STOCKS_FILE = PROJECT_ROOT / "todays_stocks.json"
LOG_DIR = PROJECT_ROOT / "logs"


def load_todays_plan() -> dict[str, str]:
    if not TODAYS_STOCKS_FILE.exists():
        raise RuntimeError(
            f"{TODAYS_STOCKS_FILE} not found. Copy todays_stocks.example.json to "
            "todays_stocks.json and fill in today's picks before running shadow mode."
        )
    with open(TODAYS_STOCKS_FILE) as f:
        plan = json.load(f)
    if not plan:
        raise RuntimeError(f"{TODAYS_STOCKS_FILE} is empty -- add at least one symbol.")
    if len(plan) > 3:
        raise RuntimeError(
            f"{TODAYS_STOCKS_FILE} has {len(plan)} symbols but max concurrent positions is 3. Trim the list."
        )
    for symbol, direction in plan.items():
        if direction not in ("long", "short"):
            raise RuntimeError(f"Invalid direction '{direction}' for {symbol} -- must be 'long' or 'short'.")
    return plan


class ShadowLogger:
    def __init__(self, log_path: Path):
        self.log_path = log_path
        log_path.parent.mkdir(exist_ok=True)

    def event(self, kind: str, **fields) -> None:
        record = {"timestamp": _now().isoformat(), "kind": kind, **fields}
        print(f"[{record['timestamp']}] {kind}: {fields}")
        with open(self.log_path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")


def run_shadow(poll_interval: int = POLL_INTERVAL_SECONDS) -> None:
    """Paper-trade the grid strategy against LIVE Kite quotes for today's plan.
    Logs every decision (entry, averaging, target exit, loss cap, square-off)
    -- it never calls any order-placement endpoint, so nothing real trades.

    Re-reads todays_stocks.json on every poll, so you can add a symbol
    (up to the 3-slot cap) any time after starting the script -- you don't
    need to know your full list before 9:15. A symbol present from the very
    start enters at the day's actual open price; a symbol that shows up
    later (you edited the file mid-morning) enters at whatever the current
    price is right then, since the day's open would no longer be a price
    you could actually have traded at. No new entries are taken within
    LATE_ENTRY_CUTOFF of square-off -- not enough of the day left for the
    strategy to do anything with a fresh position.
    """
    plan = load_todays_plan()
    original_symbols = set(plan.keys())
    kite = auth.get_kite()

    today = _now().date()
    logger = ShadowLogger(LOG_DIR / f"shadow_{today.isoformat()}.jsonl")
    symbol_atr = kite_data.fetch_symbol_atr(list(plan.keys()))
    engine = GridEngine(atr_multiplier=DEFAULT_ATR_MULTIPLIER)
    entered_today: set[str] = set()

    print(f"SHADOW MODE (paper trading, no real orders) -- {today}")
    print(f"Plan: {plan}")
    print(f"ATR (14d): {symbol_atr}")
    print(f"Log: {logger.log_path}\n")
    logger.event(
        "start", plan=plan, grid_pct=engine.grid_pct, exposure_per_unit=engine.exposure_per_unit, symbol_atr=symbol_atr
    )

    try:
        while True:
            now = _now().time()
            if now < MARKET_OPEN:
                time_module.sleep(min(poll_interval, 30))
                continue
            if now >= MARKET_CLOSE:
                print("Market closed. Ending shadow session.")
                break

            try:
                latest_plan = load_todays_plan()
            except RuntimeError as exc:
                logger.event("plan_reload_error", error=str(exc))
                latest_plan = plan
            new_symbols = set(latest_plan.keys()) - set(plan.keys())
            if new_symbols:
                try:
                    new_atr = kite_data.fetch_symbol_atr(list(new_symbols))
                    symbol_atr.update(new_atr)
                    for symbol in new_symbols:
                        logger.event("plan_updated", symbol=symbol, direction=latest_plan[symbol], atr=symbol_atr.get(symbol))
                    plan = latest_plan
                except POLL_EXCEPTIONS as exc:
                    logger.event("atr_fetch_error", symbols=list(new_symbols), error=str(exc))
                    # Keep the old plan this round (don't adopt the new symbols
                    # yet) and retry next poll -- entering without a real ATR
                    # value would silently fall back to the percentage-based
                    # trail instead of what was actually intended.
            else:
                plan = latest_plan
            # Always keep watching any symbol with an open position, even if it's
            # since been removed from today's file -- an existing paper position
            # can't just be abandoned because the file changed.
            watch_symbols = set(plan.keys()) | set(engine.open_positions.keys())
            instruments = [f"NSE:{sym}" for sym in watch_symbols]

            try:
                quotes = kite.ohlc(instruments)
            except POLL_EXCEPTIONS as exc:
                logger.event("poll_error", error=str(exc))
                time_module.sleep(poll_interval)
                continue

            current_prices: dict[str, float] = {}
            for symbol in watch_symbols:
                quote = quotes.get(f"NSE:{symbol}")
                if quote is None:
                    continue
                current_prices[symbol] = quote["last_price"]

                if symbol in plan and symbol not in entered_today and now < LATE_ENTRY_CUTOFF:
                    direction = plan[symbol]
                    is_original = symbol in original_symbols
                    entry_price = quote["ohlc"]["open"] if is_original else quote["last_price"]
                    if engine.enter(symbol, entry_price, direction, _now(), atr=symbol_atr.get(symbol)):
                        entered_today.add(symbol)
                        logger.event(
                            "entry",
                            symbol=symbol,
                            direction=direction,
                            price=entry_price,
                            atr=symbol_atr.get(symbol),
                            late_entry=not is_original,
                        )

            for symbol, price in current_prices.items():
                result = engine.update(symbol, price, _now())
                if result:
                    logger.event(result["reason"], **result)

            for result in engine.check_loss_cap(current_prices, _now()):
                logger.event("daily_loss_cap", **result)

            if now >= SQUARE_OFF_TIME and engine.open_positions:
                for result in engine.square_off(current_prices, _now()):
                    logger.event("square_off", **result)

            logger.event(
                "heartbeat",
                daily_pnl=round(engine.daily_pnl, 2),
                open_positions=list(engine.open_positions.keys()),
                halted=engine.halted,
            )

            if not engine.open_positions and (engine.halted or now >= SQUARE_OFF_TIME):
                print("All positions flat and day is done (halted or past square-off). Ending session.")
                break

            time_module.sleep(poll_interval)
    except KeyboardInterrupt:
        print("\nInterrupted by user.")

    logger.event("end", daily_pnl=round(engine.daily_pnl, 2), trades=len(engine.trade_log))
    print(f"\nShadow session ended. Net P&L for today (paper): Rs {engine.daily_pnl:,.2f}")
    print(f"Full log: {logger.log_path}")
