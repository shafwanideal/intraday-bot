import json
import math
import os
import time as time_module
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from kiteconnect.exceptions import KiteException

from . import auth, config, kite_data, orders
from .strategy import (
    AVERAGING_PCT,
    DEFAULT_ATR_MULTIPLIER,
    ENABLE_AVERAGING,
    LEVERAGE,
    LIVE_CONCURRENT_SLOTS,
    MARGIN_CAPITAL,
    MAX_STOCKS_PER_DAY,
    PORTFOLIO_PROFIT_LOCK_GIVEBACK,
    PORTFOLIO_PROFIT_LOCK_TRIGGER,
    PREMARKET_TRANCHE_PCT,
    SQUARE_OFF_TIME,
    GridEngine,
    Position,
)

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
# The day's official open is only a realistic, fillable reference price for a
# real order if we're actually placing it close to when the market opened --
# not "was this symbol in the plan file when the script started," since the
# script itself might not start until well after 9:15 (e.g. adding a stock
# to a session that's already running mid-afternoon). Using a stale open
# price hours later would size a limit order around a price the market has
# long since moved away from, and it would likely just never fill.
NEAR_OPEN_WINDOW_MINUTES = 5

CONFIRM_PHRASE = "CONFIRM"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Deliberately a SEPARATE file from shadow.py's todays_stocks.json -- the two
# must never share a stock list. Editing the shadow-mode list for paper
# trading must not be able to silently change what real orders get placed,
# and vice versa.
TODAYS_STOCKS_FILE = PROJECT_ROOT / "todays_live_stocks.json"
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


def _fetch_margin_capital(kite) -> float:
    """Size the day's trading off the account's actual available cash rather
    than the fixed MARGIN_CAPITAL default -- so depositing more (or less)
    automatically changes position sizing without a code edit. Falls back to
    the default if the margins call fails, rather than blocking a live
    session over a transient API issue."""
    try:
        cash = kite.margins()["equity"]["available"]["cash"]
        return float(cash)
    except (KeyError, TypeError, orders.PLACEMENT_EXCEPTIONS) as exc:
        print(f"WARNING: could not fetch live margin balance ({exc}); falling back to default Rs {MARGIN_CAPITAL:,}")
        return MARGIN_CAPITAL


def _fetch_realized_pnl_today(kite) -> float:
    """Today's already-realized intraday P&L (from Kite's own margin ledger),
    so a restarted session's daily_pnl starts from the true full-day total
    instead of resetting to 0. Without this, every restart makes both the
    daily loss cap and the portfolio profit lock blind to whatever was
    already booked before the restart -- found for real on 2026-08-28, where
    a restart reset ~Rs 2,609 of already-realized profit out of the profit
    lock's view entirely. Falls back to 0.0 (old behavior) if the margins
    call fails, rather than blocking a restart over a transient API issue."""
    try:
        return float(kite.margins()["equity"]["utilised"]["m2m_realised"])
    except (KeyError, TypeError, orders.PLACEMENT_EXCEPTIONS) as exc:
        print(f"WARNING: could not fetch today's realized P&L ({exc}); daily_pnl starts at 0 -- loss cap/profit lock may be inaccurate until real trades update it.")
        return 0.0


def _load_todays_plan() -> dict[str, str]:
    if not TODAYS_STOCKS_FILE.exists():
        raise RuntimeError(f"{TODAYS_STOCKS_FILE} not found.")
    with open(TODAYS_STOCKS_FILE) as f:
        plan = json.load(f)
    if not plan:
        raise RuntimeError(f"{TODAYS_STOCKS_FILE} is empty.")
    if len(plan) > MAX_STOCKS_PER_DAY:
        raise RuntimeError(f"{TODAYS_STOCKS_FILE} has {len(plan)} symbols but the max is {MAX_STOCKS_PER_DAY}.")
    for symbol, direction in plan.items():
        if direction not in ("long", "short"):
            raise RuntimeError(f"Invalid direction '{direction}' for {symbol}.")
    return plan


def _quantity_for(exposure: float, price: float) -> int:
    """Whole shares only -- real orders can't be fractional."""
    return max(0, math.floor(exposure / price))


def _reconcile_open_positions(
    kite,
    engine: GridEngine,
    symbol_atr: dict[str, float],
    real_qty: dict[str, int],
    entered_today: set[str],
    logger: "LiveLogger",
) -> None:
    """Adopt any real MIS position already open on Kite into the engine's
    bookkeeping, instead of assuming a flat book. Without this, restarting
    the script after a crash (or any interruption) mid-day would leave a
    real position completely unmonitored -- no loss-cap check, no trailing
    stop, no square-off -- until a human notices and closes it manually,
    which is exactly what happened on 2026-08-18.

    Reconstructs each position's entry leg(s) from today's actual completed
    orders (kite.orders() only returns the current trading day, which is
    exactly the window this needs) rather than trusting a single blended
    average, so averaging state (units_used, capital_units_available) stays
    correct. If reconstruction doesn't cleanly match the live net quantity,
    refuses to guess and instead halts monitoring for that symbol with a
    CRITICAL -- a wrong adopted state (e.g. wrong avg_price) is worse than
    no automated monitoring at all, since it could not just miss the true
    loss cap but confidently, silently miscalculate it.
    """
    try:
        positions = kite.positions()
    except orders.PLACEMENT_EXCEPTIONS as exc:
        logger.critical(
            f"Could not fetch existing positions from Kite to reconcile at startup: {exc}. If a real position "
            "is already open from a previous run, it will NOT be monitored until this is resolved -- check Kite directly.",
            error=str(exc),
        )
        return

    day_positions = [p for p in positions.get("day", []) if p.get("product") == "MIS" and p.get("quantity", 0) != 0]
    if not day_positions:
        return

    try:
        today_orders = kite.orders()
    except orders.PLACEMENT_EXCEPTIONS as exc:
        logger.critical(
            f"Found {len(day_positions)} open real MIS position(s) from a previous run today, but could not fetch "
            f"order history to reconstruct them: {exc}. These positions exist on Kite but will NOT be monitored "
            "by this session. Check Kite directly.",
            symbols=[p["tradingsymbol"] for p in day_positions],
        )
        return

    for p in day_positions:
        symbol = p["tradingsymbol"]
        net_qty = p["quantity"]
        direction = "long" if net_qty > 0 else "short"
        entry_side = "BUY" if direction == "long" else "SELL"
        matching = sorted(
            (
                o
                for o in today_orders
                if o.get("tradingsymbol") == symbol
                and o.get("product") == "MIS"
                and o.get("transaction_type") == entry_side
                and o.get("status") == "COMPLETE"
            ),
            key=lambda o: o.get("order_timestamp") or "",
        )
        legs = [(o["average_price"], o["filled_quantity"]) for o in matching]
        total_leg_qty = sum(q for _, q in legs)
        if not legs or total_leg_qty != abs(net_qty):
            logger.critical(
                f"{symbol} has an open real position (net qty={net_qty}) from a previous run, but reconstructing "
                f"its entry legs from today's order history didn't cleanly match (found {total_leg_qty} shares "
                f"across {len(legs)} order(s), expected {abs(net_qty)}). Refusing to guess -- this position will "
                "NOT be monitored by this session. Check Kite directly and consider closing it manually.",
                symbol=symbol,
                net_qty=net_qty,
                reconstructed_legs=legs,
            )
            continue

        if symbol not in symbol_atr:
            symbol_atr.update(kite_data.fetch_symbol_atr([symbol]))

        entry_time = matching[0].get("order_timestamp") or _now()
        engine.open_positions[symbol] = Position(
            symbol=symbol, direction=direction, entry_time=entry_time, legs=legs, atr=symbol_atr.get(symbol), averaged=len(legs) > 1
        )
        engine.capital_units_available -= len(legs)
        real_qty[symbol] = abs(net_qty)
        entered_today.add(symbol)
        avg_price = engine.open_positions[symbol].avg_price
        logger.event(
            "position_reconciled", symbol=symbol, direction=direction, avg_price=avg_price, qty=abs(net_qty), legs=legs
        )


def _detect_manual_closes(kite, engine: GridEngine, real_qty: dict[str, int], logger: "LiveLogger") -> None:
    """Our own bookkeeping only reflects orders THIS session placed -- it has
    no way to know if a position got closed (or changed) manually via the
    Kite app/website instead of through the bot's own exit logic, which is
    exactly what happened on 2026-08-24 (a manual close left the engine
    still believing a position was open for over an hour after it was
    actually flat).

    Unlike _reconcile_open_positions (startup only), this runs every poll,
    treating Kite's real position as the only source of truth. If it no
    longer matches what we expect, the position is dropped from tracking
    immediately -- the alternative (trusting stale internal state) risks the
    engine eventually placing its own real exit order against a position
    that no longer exists, which could open an accidental new real position
    in the opposite direction.
    """
    if not engine.open_positions:
        return
    try:
        real_positions = {p["tradingsymbol"]: p["quantity"] for p in kite.positions().get("day", []) if p.get("product") == "MIS"}
    except orders.PLACEMENT_EXCEPTIONS as exc:
        logger.event("manual_close_check_failed", error=str(exc))
        return

    for symbol in list(engine.open_positions.keys()):
        pos = engine.open_positions[symbol]
        expected_qty = real_qty.get(symbol, 0)
        expected_signed = expected_qty if pos.direction == "long" else -expected_qty
        actual_signed = real_positions.get(symbol, 0)
        if actual_signed != expected_signed:
            logger.critical(
                f"{symbol} real Kite position (net qty={actual_signed}) no longer matches what this session "
                f"expects (net qty={expected_signed}) -- most likely closed or modified manually outside the "
                "bot. Dropping it from automated tracking now so no phantom exit order gets placed against it.",
                symbol=symbol,
                expected=expected_signed,
                actual=actual_signed,
            )
            del engine.open_positions[symbol]
            real_qty.pop(symbol, None)


def _confirm_or_abort(
    plan: dict[str, str],
    engine: GridEngine,
    premarket_symbols: set[str],
    tranche_a_exposure: float,
    tranche_b_exposure: float,
) -> bool:
    print("=" * 70)
    print("LIVE TRADING MODE -- THIS WILL PLACE REAL ORDERS WITH REAL MONEY")
    print("=" * 70)
    print(f"Today's plan: {plan}")
    print(f"Margin capital (live, from Kite): Rs {engine.margin_capital:,.2f}")
    if premarket_symbols:
        print(
            f"Tranche A (premarket, {len(premarket_symbols)} stock(s) -- {sorted(premarket_symbols)}): "
            f"Rs {tranche_a_exposure:,.2f} exposure each"
        )
        print(f"Tranche B (anything added after open): Rs {tranche_b_exposure:,.2f} exposure each")
    else:
        print(f"No premarket tranche (session starting after market open) -- Rs {tranche_b_exposure:,.2f} exposure each")
    atr_desc = f"{engine.atr_multiplier}x" if engine.atr_multiplier is not None else "off (fixed %)"
    print(f"Daily loss cap: Rs {engine.daily_loss_cap:,.2f}  |  Grid: {engine.grid_pct:.1%}  |  ATR trail: {atr_desc}")
    print(f"Realized P&L today (seeded from Kite): Rs {engine.daily_pnl:,.2f}")
    if engine.portfolio_profit_lock_trigger is not None:
        print(
            f"Portfolio profit lock: arms at Rs {engine.portfolio_profit_lock_trigger:,.2f}, "
            f"giveback Rs {engine.portfolio_profit_lock_giveback or 0:,.2f}"
        )
    if engine.per_stock_stop_loss is not None:
        print(f"Per-stock stop-loss: Rs {engine.per_stock_stop_loss:,.2f}")
    if engine.open_positions:
        print(f"\nRECONCILED {len(engine.open_positions)} existing real position(s) from a previous run today:")
        for sym, pos in engine.open_positions.items():
            print(f"  {sym}: {pos.direction} qty={pos.qty:.0f} avg_price={pos.avg_price:.2f} legs={pos.units_used}")
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
    kite = auth.get_kite()

    today = _now().date()
    logger = LiveLogger(LOG_DIR / f"live_{today.isoformat()}.jsonl")
    symbol_atr = kite_data.fetch_symbol_atr(list(plan.keys()))
    margin_capital = _fetch_margin_capital(kite)
    # Fixed at LIVE_CONCURRENT_SLOTS from the first confirmation of the day, regardless of
    # how many stocks are given right now -- so a slot freeing up later (a position hitting
    # target) always has room for a new pick without a restart. See the constant's comment
    # for the sizing tradeoff this implies.
    max_concurrent_positions = LIVE_CONCURRENT_SLOTS
    # No spare unit reserved -- that was only ever needed for averaging's second leg, which
    # is now permanently off (ENABLE_AVERAGING). Reserving it anyway would silently shrink
    # every position by 1/(slots+1) for a leg that can never fire.
    total_units = max_concurrent_positions

    # Two-tranche capital split, adopted 2026-08-28: PREMARKET_TRANCHE_PCT of capital goes to
    # whatever's in the plan before market open (split evenly among them), the rest is held
    # back for anything added after open (split evenly across the remaining slot capacity).
    # Smaller per-order size on each half means far less chance of a margin rejection (see
    # CONCOR, 2026-08-28) than committing the whole day's capital to the first batch. If this
    # session is starting after market open already (e.g. a restart), there's no "premarket"
    # tranche left to reserve -- everything from here is tranche B.
    premarket_symbols = set(plan.keys()) if _now().time() < MARKET_OPEN else set()
    tranche_a_capital = margin_capital * PREMARKET_TRANCHE_PCT
    tranche_b_capital = margin_capital * (1 - PREMARKET_TRANCHE_PCT)
    tranche_a_count = max(len(premarket_symbols), 1)
    tranche_b_count = max(max_concurrent_positions - len(premarket_symbols), 1)
    tranche_a_exposure = (tranche_a_capital / tranche_a_count) * LEVERAGE
    tranche_b_exposure = (tranche_b_capital / tranche_b_count) * LEVERAGE

    engine = GridEngine(
        margin_capital=margin_capital,
        atr_multiplier=DEFAULT_ATR_MULTIPLIER,
        max_concurrent_positions=max_concurrent_positions,
        total_units=total_units,
        averaging_pct=AVERAGING_PCT,
        enable_averaging=ENABLE_AVERAGING,
        portfolio_profit_lock_trigger=PORTFOLIO_PROFIT_LOCK_TRIGGER,
        portfolio_profit_lock_giveback=PORTFOLIO_PROFIT_LOCK_GIVEBACK,
        # per_stock_stop_loss intentionally NOT wired in as a live default -- tested against
        # today's actual trades (2026-08-28) and it would have cut STARCEMENT right before
        # its partial recovery, making the day worse (-Rs 1,150 vs the real +Rs 33). Left
        # available in strategy.py/backtest.py for further testing, not turned on live.
    )

    entered_today: set[str] = set()
    # Authoritative record of REAL whole shares actually held per symbol,
    # confirmed from Kite fills -- never trust GridEngine's own internal qty
    # (fractional, exposure_per_unit/price) for sizing a real order. The
    # engine's qty is fine for its own P&L bookkeeping; real order quantities
    # must come from here.
    real_qty: dict[str, int] = {}

    engine.daily_pnl = _fetch_realized_pnl_today(kite)
    logger.event("daily_pnl_seeded", daily_pnl=engine.daily_pnl)

    # Opt-in, one-restart-at-a-time override: if a prior session today already breached
    # the loss cap (daily_pnl seeded below -daily_loss_cap), starting fresh as-is would
    # enter and then instantly exit every new position on the very next poll -- pure
    # churn, no benefit. Set FRESH_LOSS_BUDGET=true in the environment (not .env -- this
    # is meant to be a deliberate one-time choice per restart, not a standing default)
    # to extend the cap by however much is already realized-negative, giving exactly a
    # fresh cap-sized budget counted only from new trades onward. Does NOT erase the
    # already-realized loss -- it's still real money lost -- it only stops the cap from
    # immediately re-triggering on trades placed after this point.
    if os.environ.get("FRESH_LOSS_BUDGET", "").strip().lower() == "true":
        already_lost = max(0.0, -engine.daily_pnl)
        if already_lost > 0:
            old_cap = engine.daily_loss_cap
            engine.daily_loss_cap += already_lost
            logger.event(
                "fresh_loss_budget_applied",
                already_realized_loss=round(already_lost, 2),
                old_cap=old_cap,
                new_cap=round(engine.daily_loss_cap, 2),
            )
            print(
                f"FRESH_LOSS_BUDGET active: already realized -Rs {already_lost:,.2f} today. "
                f"Cap extended {old_cap:,.2f} -> {engine.daily_loss_cap:,.2f} so today's "
                f"NEW trades still get a full fresh budget from here."
            )

    _reconcile_open_positions(kite, engine, symbol_atr, real_qty, entered_today, logger)

    if not _confirm_or_abort(plan, engine, premarket_symbols, tranche_a_exposure, tranche_b_exposure):
        return

    logger.event(
        "start", plan=plan, grid_pct=engine.grid_pct, exposure_per_unit=engine.exposure_per_unit, symbol_atr=symbol_atr
    )

    def place_and_confirm(
        symbol: str, transaction_type: str, quantity: int, reference_price: float, tag: str, is_entry: bool = False
    ) -> dict:
        try:
            order_id = orders.place_market_order(kite, symbol, transaction_type, quantity, reference_price, tag=tag)
        except orders.OrderRejected as exc:
            if is_entry:
                # Kite cleanly rejected this -- we know for certain no position was
                # opened. Safe to just skip this one symbol; no reason to stop the
                # rest of the day's other symbols over a stock-specific rejection.
                logger.event("order_rejected", symbol=symbol, transaction_type=transaction_type, quantity=quantity, tag=tag, error=str(exc))
                return {"status": "REJECTED", "average_price": None, "filled_quantity": 0, "raw": None}
            # For anything that isn't an entry (exit/averaging/loss-cap/square-off),
            # GridEngine already updated its own bookkeeping to assume this order
            # would succeed BEFORE we attempted the real one. A clean rejection here
            # means we know FOR CERTAIN the real position is still open even though
            # the engine now thinks it's closed -- arguably worse than the ambiguous
            # case, since it's not a maybe, it's a definite mismatch. Halt either way.
            logger.critical(
                f"{symbol} {transaction_type} order was REJECTED by Kite -- the real position is definitely still "
                f"open/unclosed even though the engine's bookkeeping now assumes otherwise: {exc}",
                symbol=symbol,
                transaction_type=transaction_type,
                quantity=quantity,
                tag=tag,
            )
            engine.halted = True
            return {"status": "REJECTED", "average_price": None, "filled_quantity": 0, "raw": None}
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

            _detect_manual_closes(kite, engine, real_qty, logger)

            current_prices: dict[str, float] = {}
            for symbol in watch_symbols:
                quote = quotes.get(f"NSE:{symbol}")
                if quote is None:
                    continue
                current_prices[symbol] = quote["last_price"]

                if symbol in plan and symbol not in entered_today and now < LATE_ENTRY_CUTOFF and engine.can_enter(symbol):
                    direction = plan[symbol]
                    minutes_since_open = (
                        datetime.combine(_now().date(), now) - datetime.combine(_now().date(), MARKET_OPEN)
                    ).total_seconds() / 60
                    near_open = 0 <= minutes_since_open <= NEAR_OPEN_WINDOW_MINUTES
                    ref_price = quote["ohlc"]["open"] if near_open else quote["last_price"]
                    exposure = tranche_a_exposure if symbol in premarket_symbols else tranche_b_exposure
                    quantity = _quantity_for(exposure, ref_price)
                    if quantity < 1:
                        logger.event("entry_skipped_zero_qty", symbol=symbol, ref_price=ref_price)
                        entered_today.add(symbol)  # don't retry every poll for a stock too expensive to size
                        continue

                    transaction_type = "BUY" if direction == "long" else "SELL"
                    result = place_and_confirm(symbol, transaction_type, quantity, ref_price, tag="entry", is_entry=True)
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

            # Log the exact prices this poll is about to act on, for open positions
            # specifically -- without this, reconstructing what actually happened
            # between two 15-second polls (e.g. a fast trailing-stop trigger) after
            # the fact is only ever a best guess from retrospective 1-minute candles,
            # which don't necessarily match what the live tick feed showed in the
            # moment. Real gap on 2026-08-27 (FOSECOIND): a trailing exit fired that
            # looked impossible from the minute-candle data alone, with no way to
            # confirm what price the engine actually saw.
            if engine.open_positions:
                logger.event(
                    "poll_prices",
                    prices={sym: current_prices[sym] for sym in engine.open_positions if sym in current_prices},
                )

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
                    fill = place_and_confirm(symbol, transaction_type, sell_qty, price, tag=result["reason"])
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
                    fill = place_and_confirm(symbol, transaction_type, avg_qty, price, tag="averaging")
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
                fill = place_and_confirm(symbol, transaction_type, sell_qty, result["exit_price"], tag="daily_loss_cap")
                if fill["status"] != "COMPLETE":
                    logger.critical(
                        f"{symbol} daily-loss-cap exit did NOT confirm filled -- "
                        "real position may still be open. Check Kite directly and close it manually.",
                        symbol=symbol,
                        fill_result=fill,
                    )
                else:
                    logger.event("daily_loss_cap_exit", symbol=symbol, price=fill["average_price"], qty=fill["filled_quantity"])

            for result in engine.check_per_stock_stop_loss(current_prices, _now()):
                symbol = result["symbol"]
                sell_qty = real_qty.pop(symbol, None)
                if sell_qty is None:
                    logger.critical(
                        f"{symbol} per-stock stop-loss triggered but no real_qty on record. Check Kite directly.",
                        symbol=symbol,
                    )
                    continue
                transaction_type = "SELL" if result["direction"] == "long" else "BUY"
                fill = place_and_confirm(symbol, transaction_type, sell_qty, result["exit_price"], tag="per_stock_stop_loss")
                if fill["status"] != "COMPLETE":
                    logger.critical(
                        f"{symbol} per-stock stop-loss exit did NOT confirm filled -- "
                        "real position may still be open. Check Kite directly and close it manually.",
                        symbol=symbol,
                        fill_result=fill,
                    )
                else:
                    logger.event("per_stock_stop_loss_exit", symbol=symbol, price=fill["average_price"], qty=fill["filled_quantity"])

            for result in engine.check_portfolio_profit_lock(current_prices, _now()):
                symbol = result["symbol"]
                sell_qty = real_qty.pop(symbol, None)
                if sell_qty is None:
                    logger.critical(
                        f"{symbol} portfolio-profit-lock exit triggered but no real_qty on record. Check Kite directly.",
                        symbol=symbol,
                    )
                    continue
                transaction_type = "SELL" if result["direction"] == "long" else "BUY"
                fill = place_and_confirm(symbol, transaction_type, sell_qty, result["exit_price"], tag="portfolio_profit_lock")
                if fill["status"] != "COMPLETE":
                    logger.critical(
                        f"{symbol} portfolio-profit-lock exit did NOT confirm filled -- "
                        "real position may still be open. Check Kite directly and close it manually.",
                        symbol=symbol,
                        fill_result=fill,
                    )
                else:
                    logger.event("portfolio_profit_lock_exit", symbol=symbol, price=fill["average_price"], qty=fill["filled_quantity"])

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
                    fill = place_and_confirm(symbol, transaction_type, sell_qty, result["exit_price"], tag="square_off")
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
