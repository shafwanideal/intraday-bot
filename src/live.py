import json
import math
import time as time_module
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from kiteconnect.exceptions import KiteException

from . import auth, config, kite_data, orders
from .strategy import DEFAULT_ATR_MULTIPLIER, MARGIN_CAPITAL, SQUARE_OFF_TIME, GridEngine

IST = ZoneInfo("Asia/Kolkata")

# A poll failing (timeout, connection reset) must never crash the whole
# session -- see the equivalent constant in shadow.py for why. This matters
# even more here since a crash mid-day would leave REAL open positions
# unmonitored (no loss-cap checks, no square-off).
POLL_EXCEPTIONS = (KiteException, requests.exceptions.RequestException)


def _now() -> datetime:
    return datetime.now(IST)


MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
POLL_INTERVAL_SECONDS = 15
LATE_ENTRY_CUTOFF = time(14, 30)

CONFIRM_PHRASE = "I CONFIRM LIVE TRADING WITH REAL MONEY"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TODAYS_STOCKS_FILE = PROJECT_ROOT / "todays_stocks.json"
LOG_DIR = PROJECT_ROOT / "logs"


class LiveLogger:
    """Same shape as ShadowLogger, plus a dedicated CRITICAL channel for
    anything that needs the user's immediate manual attention (a real
    position whose exit/averaging order didn't confirm)."""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        log_path.parent.mkdir(exist_ok=True)

    def event(self, kind: str, **fields) -> None:
        record = {"timestamp": _now().isoformat(), "kind": kind, **fields}
        print(f"[{record['timestamp']}] {kind}: {fields}")
        with open(self.log_path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def critical(self, message: str, **fields) -> None:
        banner = "!" * 70
        print(f"\n{banner}\nCRITICAL -- MANUAL ACTION NEEDED: {message}\n{banner}\n")
        self.event("CRITICAL", message=message, **fields)


def _load_todays_plan() -> dict[str, str]:
    if not TODAYS_STOCKS_FILE.exists():
        raise RuntimeError(f"{TODAYS_STOCKS_FILE} not found.")
    with open(TODAYS_STOCKS_FILE) as f:
        plan = json.load(f)
    if not plan:
        raise RuntimeError(f"{TODAYS_STOCKS_FILE} is empty.")
    if len(plan) > 3:
        raise RuntimeError(f"{TODAYS_STOCKS_FILE} has {len(plan)} symbols but max concurrent positions is 3.")
    for symbol, direction in plan.items():
        if direction not in ("long", "short"):
            raise RuntimeError(f"Invalid direction '{direction}' for {symbol}.")
    return plan


def _quantity_for(exposure: float, price: float) -> int:
    """Whole shares only -- real orders can't be fractional."""
    return max(0, math.floor(exposure / price))


def _confirm_or_abort(plan: dict[str, str], engine: GridEngine) -> bool:
    print("=" * 70)
    print("LIVE TRADING MODE -- THIS WILL PLACE REAL ORDERS WITH REAL MONEY")
    print("=" * 70)
    print(f"Today's plan: {plan}")
    print(f"Margin capital: Rs {MARGIN_CAPITAL:,}  |  Exposure per unit: Rs {engine.exposure_per_unit:,.2f}")
    atr_desc = f"{engine.atr_multiplier}x" if engine.atr_multiplier is not None else "off (fixed %)"
    print(f"Daily loss cap: Rs {engine.daily_loss_cap:,}  |  Grid: {engine.grid_pct:.1%}  |  ATR trail: {atr_desc}")
    print()
    typed = input(f'Type exactly "{CONFIRM_PHRASE}" to proceed, anything else aborts: ')
    if typed.strip() != CONFIRM_PHRASE:
        print("Confirmation did not match -- aborting. No orders placed.")
        return False
    return True


def run_live(poll_interval: int = POLL_INTERVAL_SECONDS) -> None:
    """Trade the grid strategy with REAL orders against today's plan.

    Safety gates, all of which must pass before a single order is placed:
    1. LIVE_TRADING_ENABLED=true must be explicitly set in .env.
    2. The user must type an exact confirmation phrase interactively.

    Entries use check-then-place-then-commit: GridEngine.can_enter() is a
    pure query, so we place the real order first and only record the
    position in the engine (using the REAL fill price) once Kite confirms
    it filled. If the order is rejected or times out, the engine's state is
    never touched -- there is no risk of it believing a position exists that
    doesn't.

    Averaging and exits (target/trailing/loss-cap/square-off) work the other
    way: GridEngine decides and updates its own bookkeeping using the polled
    price (this reuses the exact same logic already validated in backtest
    and shadow mode), and this function immediately places the matching real
    order after. If that real order does NOT confirm filled, this is
    reported as CRITICAL and the engine is halted -- a mismatch between what
    the engine thinks happened and what the market actually did is exactly
    the failure mode that needs a human, not more automation, so the safe
    response is to stop and surface it loudly rather than guess.
    """
    if not config.LIVE_TRADING_ENABLED:
        raise RuntimeError(
            "LIVE_TRADING_ENABLED is not set to 'true' in .env. This is a deliberate safety "
            "gate -- add it explicitly if you actually intend to place real orders today."
        )

    plan = _load_todays_plan()
    original_symbols = set(plan.keys())
    kite = auth.get_kite()

    today = _now().date()
    logger = LiveLogger(LOG_DIR / f"live_{today.isoformat()}.jsonl")
    symbol_atr = kite_data.fetch_symbol_atr(list(plan.keys()))
    engine = GridEngine(atr_multiplier=DEFAULT_ATR_MULTIPLIER)

    if not _confirm_or_abort(plan, engine):
        return

    entered_today: set[str] = set()
    # Authoritative record of REAL whole shares actually held per symbol,
    # confirmed from Kite fills -- never trust GridEngine's own internal qty
    # (fractional, exposure_per_unit/price) for sizing a real order. The
    # engine's qty is fine for its own P&L bookkeeping; real order quantities
    # must come from here.
    real_qty: dict[str, int] = {}
    logger.event(
        "start", plan=plan, grid_pct=engine.grid_pct, exposure_per_unit=engine.exposure_per_unit, symbol_atr=symbol_atr
    )

    def place_and_confirm(symbol: str, transaction_type: str, quantity: int, tag: str) -> dict:
        try:
            order_id = orders.place_market_order(kite, symbol, transaction_type, quantity, tag=tag)
        except orders.OrderPlacementAmbiguous as exc:
            logger.critical(
                f"{symbol} {transaction_type} order placement is in an UNKNOWN state -- {exc}",
                symbol=symbol,
                transaction_type=transaction_type,
                quantity=quantity,
                tag=tag,
            )
            engine.halted = True
            return {"status": "AMBIGUOUS", "average_price": None, "filled_quantity": 0, "raw": None}
        logger.event("order_placed", symbol=symbol, transaction_type=transaction_type, quantity=quantity, order_id=order_id, tag=tag)
        result = orders.wait_for_fill(kite, order_id)
        logger.event("order_result", symbol=symbol, order_id=order_id, **{k: v for k, v in result.items() if k != "raw"})
        return result

    try:
        while True:
            now = _now().time()
            if now < MARKET_OPEN:
                time_module.sleep(min(poll_interval, 30))
                continue
            if now >= MARKET_CLOSE:
                print("Market closed. Ending live session.")
                break

            try:
                latest_plan = _load_todays_plan()
            except RuntimeError as exc:
                logger.event("plan_reload_error", error=str(exc))
                latest_plan = plan
            new_symbols = set(latest_plan.keys()) - set(plan.keys())
            if new_symbols:
                try:
                    symbol_atr.update(kite_data.fetch_symbol_atr(list(new_symbols)))
                    plan = latest_plan
                except POLL_EXCEPTIONS as exc:
                    logger.event("atr_fetch_error", symbols=list(new_symbols), error=str(exc))
                    # Keep the old plan this round, retry next poll -- see shadow.py.
            else:
                plan = latest_plan
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

                if symbol in plan and symbol not in entered_today and now < LATE_ENTRY_CUTOFF and engine.can_enter(symbol):
                    direction = plan[symbol]
                    is_original = symbol in original_symbols
                    ref_price = quote["ohlc"]["open"] if is_original else quote["last_price"]
                    quantity = _quantity_for(engine.exposure_per_unit, ref_price)
                    if quantity < 1:
                        logger.event("entry_skipped_zero_qty", symbol=symbol, ref_price=ref_price)
                        entered_today.add(symbol)  # don't retry every poll for a stock too expensive to size
                        continue

                    transaction_type = "BUY" if direction == "long" else "SELL"
                    result = place_and_confirm(symbol, transaction_type, quantity, tag="entry")
                    entered_today.add(symbol)  # one entry attempt per symbol per day, win or lose
                    if result["status"] == "COMPLETE" and result["average_price"]:
                        filled_qty = result["filled_quantity"] or quantity
                        engine.enter(
                            symbol, result["average_price"], direction, _now(), atr=symbol_atr.get(symbol), quantity=filled_qty
                        )
                        real_qty[symbol] = filled_qty
                        logger.event("entry", symbol=symbol, direction=direction, price=result["average_price"], quantity=filled_qty)
                    else:
                        logger.event("entry_failed", symbol=symbol, direction=direction, quantity=quantity, result=result)

            # Averaging: detect the transition via before/after state, since
            # GridEngine.update() mutates pos.averaged internally rather than
            # returning it as an event.
            pre_averaged = {sym: pos.averaged for sym, pos in engine.open_positions.items()}

            for symbol, price in current_prices.items():
                result = engine.update(symbol, price, _now())
                if result:
                    # A close happened (target/trailing exit) -- place the real matching
                    # order, sized from our own real_qty tracking, not the engine's
                    # internal (possibly fractional) qty.
                    sell_qty = real_qty.pop(symbol, None)
                    if sell_qty is None:
                        logger.critical(
                            f"{symbol} exit triggered but no real_qty on record -- cannot safely size the order. "
                            "This should not happen; check Kite directly for any open position.",
                            symbol=symbol,
                        )
                        engine.halted = True
                        continue
                    transaction_type = "SELL" if result["direction"] == "long" else "BUY"
                    fill = place_and_confirm(symbol, transaction_type, sell_qty, tag=result["reason"])
                    if fill["status"] != "COMPLETE":
                        engine.halted = True
                        logger.critical(
                            f"{symbol} exit order ({result['reason']}) did NOT confirm filled -- "
                            "engine's bookkeeping now says this position is closed, but the real "
                            "position may still be open. Check Kite directly and close it manually if needed.",
                            symbol=symbol,
                            fill_result=fill,
                        )
                    else:
                        logger.event("exit", symbol=symbol, reason=result["reason"], price=fill["average_price"], qty=fill["filled_quantity"])
                elif symbol in engine.open_positions and not pre_averaged.get(symbol, False) and engine.open_positions[symbol].averaged:
                    # Averaging just triggered inside update() -- it appended a leg using
                    # the engine's own fractional qty (exposure/price), which is NOT a
                    # real order size. Compute the real whole-share quantity for the
                    # averaging leg ourselves, place that, then overwrite the leg the
                    # engine already added so its bookkeeping reflects reality.
                    pos = engine.open_positions[symbol]
                    avg_qty = _quantity_for(engine.exposure_per_unit, price)
                    if avg_qty < 1:
                        logger.critical(
                            f"{symbol} averaging triggered but price {price} is too high to size a whole-share "
                            "order -- engine already recorded a fractional leg internally. Halting; check Kite directly.",
                            symbol=symbol,
                        )
                        engine.halted = True
                        continue
                    transaction_type = "BUY" if pos.direction == "long" else "SELL"
                    fill = place_and_confirm(symbol, transaction_type, avg_qty, tag="averaging")
                    if fill["status"] != "COMPLETE":
                        engine.halted = True
                        logger.critical(
                            f"{symbol} averaging order did NOT confirm filled -- engine now assumes "
                            "a second leg exists that may not really be there. Check Kite directly.",
                            symbol=symbol,
                            fill_result=fill,
                        )
                    else:
                        real_filled = fill["filled_quantity"] or avg_qty
                        pos.legs[-1] = (fill["average_price"], real_filled)  # correct the leg to the REAL fill
                        real_qty[symbol] = real_qty.get(symbol, 0) + real_filled
                        logger.event("averaging", symbol=symbol, price=fill["average_price"], qty=real_filled)

            for result in engine.check_loss_cap(current_prices, _now()):
                symbol = result["symbol"]
                sell_qty = real_qty.pop(symbol, None)
                if sell_qty is None:
                    logger.critical(
                        f"{symbol} daily-loss-cap exit triggered but no real_qty on record. Check Kite directly.",
                        symbol=symbol,
                    )
                    continue
                transaction_type = "SELL" if result["direction"] == "long" else "BUY"
                fill = place_and_confirm(symbol, transaction_type, sell_qty, tag="daily_loss_cap")
                if fill["status"] != "COMPLETE":
                    logger.critical(
                        f"{symbol} daily-loss-cap exit did NOT confirm filled -- "
                        "real position may still be open. Check Kite directly and close it manually.",
                        symbol=symbol,
                        fill_result=fill,
                    )
                else:
                    logger.event("daily_loss_cap_exit", symbol=symbol, price=fill["average_price"], qty=fill["filled_quantity"])

            if now >= SQUARE_OFF_TIME and engine.open_positions:
                for result in engine.square_off(current_prices, _now()):
                    symbol = result["symbol"]
                    sell_qty = real_qty.pop(symbol, None)
                    if sell_qty is None:
                        logger.critical(
                            f"{symbol} SQUARE-OFF triggered but no real_qty on record -- "
                            "position may still be open past market close. Check Kite directly RIGHT NOW.",
                            symbol=symbol,
                        )
                        continue
                    transaction_type = "SELL" if result["direction"] == "long" else "BUY"
                    fill = place_and_confirm(symbol, transaction_type, sell_qty, tag="square_off")
                    if fill["status"] != "COMPLETE":
                        logger.critical(
                            f"{symbol} SQUARE-OFF order did NOT confirm filled -- "
                            "this position may still be open past market close. Check Kite directly RIGHT NOW.",
                            symbol=symbol,
                            fill_result=fill,
                        )
                    else:
                        logger.event("square_off", symbol=symbol, price=fill["average_price"], qty=fill["filled_quantity"])

            logger.event(
                "heartbeat",
                daily_pnl=round(engine.daily_pnl, 2),
                open_positions=list(engine.open_positions.keys()),
                halted=engine.halted,
            )

            if not engine.open_positions and (engine.halted or now >= SQUARE_OFF_TIME):
                print("All positions flat and day is done. Ending session.")
                break

            time_module.sleep(poll_interval)
    except KeyboardInterrupt:
        print("\nInterrupted by user. Any open real positions are NOT automatically closed -- check Kite directly.")

    logger.event("end", daily_pnl=round(engine.daily_pnl, 2), trades=len(engine.trade_log))
    print(f"\nLive session ended. Realized net P&L for today: Rs {engine.daily_pnl:,.2f}")
    print(f"Full log: {logger.log_path}")
