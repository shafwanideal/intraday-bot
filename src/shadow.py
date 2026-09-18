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
from .strategy import (
    AVERAGING_PCT,
    ENABLE_AVERAGING,
    LEVERAGE,
    LIVE_CONCURRENT_SLOTS,
    MARGIN_CAPITAL,
    MAX_STOCKS_PER_DAY,
    PREMARKET_TRANCHE_PCT,
    PROFIT_EXIT,
    SQUARE_OFF_TIME,
    TRAIL_STOP,
    GridEngine,
    compute_daily_profit_target,
    compute_portfolio_profit_lock_trigger,
)

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


def _fetch_margin_capital(kite) -> float:
    """Paper-trade sizing should mirror live.py's real sizing (actual
    available cash), not the fixed default -- otherwise shadow-mode P&L
    isn't a realistic preview of what a live session would actually do."""
    try:
        cash = kite.margins()["equity"]["available"]["cash"]
        return float(cash)
    except (KeyError, TypeError, *POLL_EXCEPTIONS) as exc:
        print(f"WARNING: could not fetch live margin balance ({exc}); falling back to default Rs {MARGIN_CAPITAL:,}")
        return MARGIN_CAPITAL


def _parse_plan_entry(symbol: str, entry) -> tuple[str, float | None]:
    """Mirrors src/live.py's _parse_plan_entry -- see there for the full
    rationale. A plan-file entry is either a plain direction string (equal
    split, original behavior) or {"direction": ..., "pct": ...} to size that
    position at an explicit percentage of margin_capital."""
    if isinstance(entry, str):
        if entry not in ("long", "short"):
            raise RuntimeError(f"Invalid direction '{entry}' for {symbol} -- must be 'long' or 'short'.")
        return entry, None
    if not isinstance(entry, dict):
        raise RuntimeError(f"Invalid plan entry for {symbol}: {entry!r} -- must be 'long'/'short' or a {{direction, pct}} object.")
    direction = entry.get("direction")
    if direction not in ("long", "short"):
        raise RuntimeError(f"Invalid direction '{direction}' for {symbol} -- must be 'long' or 'short'.")
    pct = entry.get("pct")
    if pct is not None:
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            raise RuntimeError(f"pct for {symbol} must be a number, got {pct!r}.")
        if not (0 < pct <= 100):
            raise RuntimeError(f"pct for {symbol} must be between 0 and 100, got {pct}.")
    return direction, pct


def _validate_allocations(plan: dict[str, str], allocations: dict[str, float]) -> None:
    """Mirrors src/live.py's _validate_allocations -- all-or-nothing per day."""
    if not allocations:
        return
    missing = sorted(set(plan) - set(allocations))
    if missing:
        raise RuntimeError(
            f"Some symbols have an explicit pct allocation and some don't: {missing} are missing one. "
            "Give every symbol a pct, or none at all (equal split)."
        )
    total_pct = sum(allocations.values())
    if total_pct > 100 + 1e-9:
        raise RuntimeError(f"Percentages sum to {total_pct:.2f}%, which is over 100% of margin capital.")


def load_todays_plan() -> tuple[dict[str, str], dict[str, float]]:
    """Returns (plan, allocations) -- see src/live.py's _load_todays_plan
    docstring. Empty allocations means equal split, unchanged behavior."""
    if not TODAYS_STOCKS_FILE.exists():
        raise RuntimeError(
            f"{TODAYS_STOCKS_FILE} not found. Copy todays_stocks.example.json to "
            "todays_stocks.json and fill in today's picks before running shadow mode."
        )
    with open(TODAYS_STOCKS_FILE) as f:
        raw = json.load(f)
    if not raw:
        raise RuntimeError(f"{TODAYS_STOCKS_FILE} is empty -- add at least one symbol.")
    if len(raw) > MAX_STOCKS_PER_DAY:
        raise RuntimeError(
            f"{TODAYS_STOCKS_FILE} has {len(raw)} symbols but the max is {MAX_STOCKS_PER_DAY}. Trim the list."
        )
    plan: dict[str, str] = {}
    allocations: dict[str, float] = {}
    for symbol, entry in raw.items():
        direction, pct = _parse_plan_entry(symbol, entry)
        plan[symbol] = direction
        if pct is not None:
            allocations[symbol] = pct
    _validate_allocations(plan, allocations)
    return plan, allocations


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
    plan, allocations = load_todays_plan()
    original_symbols = set(plan.keys())
    kite = auth.get_kite()

    today = _now().date()
    logger = ShadowLogger(LOG_DIR / f"shadow_{today.isoformat()}.jsonl")
    symbol_atr = kite_data.fetch_symbol_atr(list(plan.keys()))
    margin_capital = _fetch_margin_capital(kite)
    max_concurrent_positions = LIVE_CONCURRENT_SLOTS
    total_units = max_concurrent_positions  # no spare unit -- averaging is permanently off

    premarket_symbols = set(plan.keys()) if _now().time() < MARKET_OPEN else set()
    tranche_a_capital = margin_capital * PREMARKET_TRANCHE_PCT
    tranche_b_capital = margin_capital * (1 - PREMARKET_TRANCHE_PCT)
    tranche_a_count = max(len(premarket_symbols), 1)
    tranche_b_count = max(max_concurrent_positions - len(premarket_symbols), 1)
    tranche_a_exposure = (tranche_a_capital / tranche_a_count) * LEVERAGE
    tranche_b_exposure = (tranche_b_capital / tranche_b_count) * LEVERAGE

    engine = GridEngine(
        margin_capital=margin_capital,
        # atr_multiplier intentionally NOT set -- see live.py's identical comment;
        # falls back to trail_pct (flat 0.75%, grid_pct/2 by default).
        max_concurrent_positions=max_concurrent_positions,
        total_units=total_units,
        averaging_pct=AVERAGING_PCT,
        enable_averaging=ENABLE_AVERAGING,
        trail_stop=TRAIL_STOP,
        profit_exit=PROFIT_EXIT,
        # 2.667% of the day's real margin_capital, not a fixed rupee figure -- see
        # compute_portfolio_profit_lock_trigger. fixed=True: pinned floor, no ratchet --
        # see live.py's identical comment on check_portfolio_profit_lock.
        portfolio_profit_lock_trigger=compute_portfolio_profit_lock_trigger(margin_capital),
        portfolio_profit_lock_fixed=True,
        # Hard take-profit ceiling on top of the profit lock above -- see live.py's
        # identical wiring and strategy.py's check_daily_profit_target.
        daily_profit_target=compute_daily_profit_target(margin_capital),
        # per_stock_stop_loss intentionally NOT wired in as a live default -- see live.py.
    )
    entered_today: set[str] = set()

    print(f"SHADOW MODE (paper trading, no real orders) -- {today}")
    print(f"Plan: {plan}")
    print(f"Margin capital (live, from Kite): Rs {margin_capital:,.2f}")
    if allocations:
        print("Allocation (explicit % of margin capital):")
        for symbol, direction in plan.items():
            pct = allocations.get(symbol)
            exposure = pct / 100.0 * margin_capital * LEVERAGE
            print(f"  {symbol:12s} {direction:5s} {pct:5.1f}%  -> Rs {exposure:,.2f} exposure")
    elif premarket_symbols:
        print(f"Tranche A (premarket, {sorted(premarket_symbols)}): Rs {tranche_a_exposure:,.2f} each")
        print(f"Tranche B (added after open): Rs {tranche_b_exposure:,.2f} each")
    else:
        print(f"No premarket tranche -- Rs {tranche_b_exposure:,.2f} each")
    print(f"Daily loss cap: Rs {engine.daily_loss_cap:,.2f}")
    print(f"ATR (14d): {symbol_atr}")
    print(f"Log: {logger.log_path}\n")
    logger.event(
        "start",
        plan=plan,
        grid_pct=engine.grid_pct,
        margin_capital=margin_capital,
        exposure_per_unit=engine.exposure_per_unit,
        daily_loss_cap=engine.daily_loss_cap,
        symbol_atr=symbol_atr,
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
                latest_plan, latest_allocations = load_todays_plan()
            except RuntimeError as exc:
                logger.event("plan_reload_error", error=str(exc))
                latest_plan, latest_allocations = plan, allocations
            new_symbols = set(latest_plan.keys()) - set(plan.keys())
            if new_symbols:
                try:
                    new_atr = kite_data.fetch_symbol_atr(list(new_symbols))
                    symbol_atr.update(new_atr)
                    for symbol in new_symbols:
                        logger.event("plan_updated", symbol=symbol, direction=latest_plan[symbol], atr=symbol_atr.get(symbol))
                    plan = latest_plan
                    allocations = latest_allocations  # see live.py's identical comment
                except POLL_EXCEPTIONS as exc:
                    logger.event("atr_fetch_error", symbols=list(new_symbols), error=str(exc))
                    # Keep the old plan this round (don't adopt the new symbols
                    # yet) and retry next poll -- entering without a real ATR
                    # value would silently fall back to the percentage-based
                    # trail instead of what was actually intended.
            else:
                plan = latest_plan
                allocations = latest_allocations
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
                    if symbol in allocations:
                        exposure = allocations[symbol] / 100.0 * margin_capital * LEVERAGE
                    else:
                        exposure = tranche_a_exposure if symbol in premarket_symbols else tranche_b_exposure
                    quantity = exposure / entry_price
                    if engine.enter(symbol, entry_price, direction, _now(), atr=symbol_atr.get(symbol), quantity=quantity):
                        entered_today.add(symbol)
                        logger.event(
                            "entry",
                            symbol=symbol,
                            direction=direction,
                            price=entry_price,
                            atr=symbol_atr.get(symbol),
                            late_entry=not is_original,
                        )

            if engine.open_positions:
                logger.event(
                    "poll_prices",
                    prices={sym: current_prices[sym] for sym in engine.open_positions if sym in current_prices},
                )

            for symbol, price in current_prices.items():
                result = engine.update(symbol, price, _now())
                if result:
                    logger.event(result["reason"], **result)

            for result in engine.check_loss_cap(current_prices, _now()):
                logger.event("daily_loss_cap", **result)

            for result in engine.check_daily_profit_target(current_prices, _now()):
                logger.event("daily_profit_target", **result)

            for result in engine.check_per_stock_stop_loss(current_prices, _now()):
                logger.event("per_stock_stop_loss", **result)

            for result in engine.check_portfolio_profit_lock(current_prices, _now()):
                logger.event("portfolio_profit_lock", **result)

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
