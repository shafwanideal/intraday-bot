"""Live execution for the swing/positional strategy (src/swing_strategy.py) --
places REAL Zerodha equity DELIVERY (CNC) orders, not MIS. Separate from
src/live.py in almost every mechanical way, same as swing_strategy.SwingEngine
is deliberately separate from strategy.GridEngine:

- Positions persist across MULTIPLE DAYS, not reset every morning -- so state
  lives in its own JSON file (SWING_STATE_FILE), read and rewritten on every
  run, rather than reconstructed from Kite's order history the way live.py
  does (kite.orders()/kite.positions() only ever cover the CURRENT trading
  day, which can't tell us anything about a position opened last week).
- Meant to run ONCE PER DAY, at or after market close (so "today's bar" is a
  genuine final close/low/open, not a still-forming one) -- not continuously
  polled like run_live.py's intraday loop. Run manually, or on a daily cron/
  systemd timer; see scripts/run_swing_live.py.
- Long-only, no stop-loss, no cap on averaging legs by default -- see
  swing_strategy.py's own docstring for why that's deliberate, not an
  oversight. An optional SWING_MAX_TOTAL_CAPITAL_OVERRIDE env var (same
  one-time-override convention as run_live.py's *_OVERRIDE vars) can cap
  total committed capital for a run without touching code.

Two different real-money actions get two different safety postures here,
mirroring exactly how run_live.py treats entries vs. exits/averaging:

- A BRAND NEW first leg on a watchlist symbol, or an AVERAGING leg on an
  already-open position, commits FRESH capital -- both are staged and shown
  to a human, who must type CONFIRM before any real order goes out (same
  CONFIRM_PHRASE gate as run_live.py). Uses check-then-place-then-commit:
  the real order is placed first, and its REAL fill price/qty -- not a
  pre-order estimate -- is what gets recorded into state.
- An EXIT (trailing-stop fill) only ever closes an already-open position --
  no new capital at risk -- so it is placed automatically, same safety
  argument as run_live.py's protect_only mode. SwingEngine.update() has
  already decided to close before this module sees it; if the real SELL
  order then fails to confirm, that's CRITICAL and halts processing for that
  symbol -- a mismatch between engine bookkeeping and real holdings is
  exactly the failure mode that needs a human, not more automation.
"""

import json
import math
import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import auth, config, kite_data, orders
from .swing_strategy import (
    AVERAGING_DROP_PCT,
    CAPITAL_PER_LEG,
    PROFIT_TARGET_PCT,
    TRAILING_PCT,
    SwingEngine,
    SwingPosition,
)

IST = ZoneInfo("Asia/Kolkata")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Deliberately separate from both todays_live_stocks.json (intraday MIS) and
# todays_stocks.json (intraday shadow) -- this is a different product type
# (CNC) with a different lifecycle (multi-day) and must never be mixed up
# with either.
SWING_STATE_FILE = PROJECT_ROOT / "swing_positions_state.json"
SWING_WATCHLIST_FILE = PROJECT_ROOT / "swing_watchlist.json"
LOG_DIR = PROJECT_ROOT / "logs"

CONFIRM_PHRASE = "CONFIRM"


def _now() -> datetime:
    return datetime.now(IST)


class SwingLogger:
    """Same jsonl-plus-optional-notifier shape as live.py's LiveLogger, kept
    as a separate class (not shared) so a change to one can't silently affect
    the other's behavior."""

    NOTIFY_KINDS = {
        "swing_entry",
        "swing_averaging",
        "swing_exit",
        "start",
        "end",
        "confirmation_declined",
    }

    def __init__(self, log_path: Path, notifier=None):
        self.log_path = log_path
        self.notifier = notifier
        log_path.parent.mkdir(exist_ok=True)

    def _notify(self, text: str) -> None:
        if self.notifier is None:
            return
        try:
            self.notifier(text)
        except Exception as exc:  # noqa: BLE001 - a chat outage must never stop trading
            print(f"WARNING: notifier failed: {exc}")

    def event(self, kind: str, **fields) -> None:
        record = {"timestamp": _now().isoformat(), "kind": kind, **fields}
        print(f"[{record['timestamp']}] {kind}: {fields}")
        with open(self.log_path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
        if kind in self.NOTIFY_KINDS:
            self._notify(f"{kind}: {fields}")

    def critical(self, message: str, **fields) -> None:
        banner = "!" * 70
        print(f"\n{banner}\nCRITICAL -- MANUAL ACTION NEEDED: {message}\n{banner}\n")
        self.event("CRITICAL", message=message, **fields)
        # Routed outside NOTIFY_KINDS so a CRITICAL can never be filtered out
        # by an edit to that set -- this is the one message that must arrive.
        self._notify(f"⚠️ SWING CRITICAL -- MANUAL ACTION NEEDED\n\n{message}")


def load_watchlist() -> list[str]:
    """Symbols currently being considered for a brand-new swing entry. Plain
    list, no direction field -- the strategy is long-only (see
    swing_strategy.py). Managed via scripts/swing_add.py or by editing the
    file directly; a symbol already open is simply skipped each run, not
    auto-removed, so re-adding after a close is harmless."""
    if not SWING_WATCHLIST_FILE.exists():
        return []
    with open(SWING_WATCHLIST_FILE) as f:
        return json.load(f)


def load_state(engine: SwingEngine) -> None:
    """Populate engine.open_positions from SWING_STATE_FILE. A missing file
    means no open swing positions yet (first run ever) -- not an error."""
    if not SWING_STATE_FILE.exists():
        return
    with open(SWING_STATE_FILE) as f:
        raw = json.load(f)
    for symbol, data in raw.get("positions", {}).items():
        legs = [(p, q, date.fromisoformat(d)) for p, q, d in data["legs"]]
        engine.open_positions[symbol] = SwingPosition(
            symbol=symbol,
            legs=legs,
            entry_qty=data["entry_qty"],
            trailing=data.get("trailing", False),
            peak_price=data.get("peak_price"),
            atr=data.get("atr"),
        )


def save_state(engine: SwingEngine) -> None:
    """Overwrite SWING_STATE_FILE with the engine's current open positions.
    Called after every exit and every confirmed real fill, so a crash
    mid-run never loses more than the single in-flight order -- there is no
    in-memory-only window where a real fill exists but the state file
    doesn't know about it yet."""
    positions = {
        symbol: {
            "legs": [[p, q, d.isoformat()] for p, q, d in pos.legs],
            "entry_qty": pos.entry_qty,
            "trailing": pos.trailing,
            "peak_price": pos.peak_price,
            "atr": pos.atr,
        }
        for symbol, pos in engine.open_positions.items()
    }
    with open(SWING_STATE_FILE, "w") as f:
        json.dump({"positions": positions, "updated_at": _now().isoformat()}, f, indent=2)


def _latest_daily_bar(symbol: str) -> dict | None:
    """Today's (or the most recent available) Open/Low/Close for `symbol`,
    via Kite's daily historical candles. Meant to be called at/after market
    close so the last row genuinely IS today's final bar, not a
    still-forming one -- see this module's own docstring."""
    df = kite_data.fetch_daily(symbol, days=5)
    if df.empty:
        return None
    last = df.iloc[-1]
    return {
        "date": df.index[-1].date(),
        "open": float(last["Open"]),
        "low": float(last["Low"]),
        "close": float(last["Close"]),
    }


def _confirm_or_abort(summary: str) -> bool:
    print(summary)
    print()
    typed = input(f'Type exactly "{CONFIRM_PHRASE}" to proceed, anything else aborts: ')
    if typed.strip() != CONFIRM_PHRASE:
        print("Confirmation did not match -- aborting. No new orders placed.")
        return False
    return True


def run_swing_check(confirm_fn=None, notifier=None) -> None:
    """One daily pass over the swing book:

    1. Resolve every open position against today's bar -- auto-execute any
       trailing-stop exit (no new capital, safe to automate); stage (but do
       NOT yet place) any averaging leg the engine's own rules call for.
    2. Check the watchlist for symbols eligible for a brand-new first leg.
    3. If anything from 1-2 needs fresh capital, show it all to a human and
       require CONFIRM before placing a single real BUY order.

    See this module's docstring for the full reasoning behind the
    auto-exit / human-approves-entries split.
    """
    if not config.LIVE_TRADING_ENABLED:
        raise RuntimeError(
            "LIVE_TRADING_ENABLED is not set to 'true' in .env. This is a deliberate safety "
            "gate -- add it explicitly if you actually intend to place real swing orders today."
        )

    kite = auth.get_kite()
    today = _now().date()
    logger = SwingLogger(LOG_DIR / f"swing_{today.isoformat()}.jsonl", notifier=notifier)

    engine = SwingEngine(
        capital_per_leg=CAPITAL_PER_LEG,
        averaging_drop_pct=AVERAGING_DROP_PCT,
        profit_target_pct=PROFIT_TARGET_PCT,
        trailing_pct=TRAILING_PCT,
        apply_costs=True,
        # A breached trailing stop is a resting order -- fill it at the stop level
        # (or the day's open on a gap-down), not at the close, same reasoning as
        # SwingEngine's own comment and src/swing_backtest.py's default.
        stop_fill_at_level=True,
        max_total_capital=(
            float(os.environ["SWING_MAX_TOTAL_CAPITAL_OVERRIDE"])
            if os.environ.get("SWING_MAX_TOTAL_CAPITAL_OVERRIDE", "").strip()
            else None
        ),
    )
    load_state(engine)
    watchlist = load_watchlist()

    logger.event("start", open_positions=list(engine.open_positions.keys()), watchlist=watchlist)

    # ---- Pass 1: update every open position off today's bar. ----
    # Exits are placed for real immediately (no new capital at risk). An
    # averaging leg is FRESH capital -- SwingEngine.update() mutates pos.legs
    # for free (it assumes a backtest fill), so that mutation is undone here
    # and staged for the CONFIRM step below instead of trusted as-is.
    pending_averaging: dict[str, tuple[float, int, date]] = {}
    for symbol in list(engine.open_positions.keys()):
        bar = _latest_daily_bar(symbol)
        if bar is None:
            logger.event("swing_bar_fetch_failed", symbol=symbol)
            continue
        pos = engine.open_positions[symbol]
        legs_before = len(pos.legs)
        result = engine.update(symbol, bar["close"], bar["date"], low=bar["low"], open_=bar["open"])

        if result is not None:
            try:
                order_id = orders.place_delivery_order(
                    kite, symbol, "SELL", result["qty"], result["exit_price"], tag="swing_exit"
                )
            except orders.OrderRejected as exc:
                logger.critical(
                    f"{symbol} swing EXIT order was REJECTED by Kite -- the real position is almost certainly "
                    f"still open even though bookkeeping now assumes it's closed: {exc}",
                    symbol=symbol,
                )
                continue
            except orders.OrderPlacementAmbiguous as exc:
                logger.critical(f"{symbol} swing EXIT order placement is in an UNKNOWN state -- {exc}", symbol=symbol)
                continue
            fill = orders.wait_for_fill(kite, order_id)
            if fill["status"] != "COMPLETE":
                logger.critical(
                    f"{symbol} swing EXIT did not confirm filled -- real position may still be open. "
                    "Check Kite directly and close it manually if needed.",
                    symbol=symbol,
                    fill_result=fill,
                )
                continue
            logger.event(
                "swing_exit",
                symbol=symbol,
                qty=fill["filled_quantity"],
                price=fill["average_price"],
                pnl=round(result["pnl"], 2),
                reason=result["reason"],
            )
        elif len(pos.legs) > legs_before:
            pending_averaging[symbol] = pos.legs.pop()

        save_state(engine)

    # ---- Pass 2: brand-new entries from the watchlist. ----
    pending_entries: dict[str, float] = {}
    for symbol in watchlist:
        if symbol in engine.open_positions or symbol in pending_averaging:
            continue
        bar = _latest_daily_bar(symbol)
        if bar is None:
            logger.event("swing_bar_fetch_failed", symbol=symbol)
            continue
        if engine.can_enter(symbol):
            pending_entries[symbol] = bar["close"]

    if not pending_entries and not pending_averaging:
        logger.event("end", new_entries=0, averaging_legs=0)
        return

    # ---- Human approval gate for anything that commits fresh capital. ----
    lines = [
        "=" * 70,
        "SWING TRADING -- THIS WILL PLACE REAL DELIVERY (CNC) ORDERS WITH REAL MONEY",
        "=" * 70,
    ]
    for symbol, price in pending_entries.items():
        qty = math.floor(CAPITAL_PER_LEG / price)
        lines.append(f"NEW ENTRY     {symbol:12s} buy {qty} @ ~Rs {price:,.2f}  (~Rs {qty * price:,.2f})")
    for symbol, (price, qty, _) in pending_averaging.items():
        leg_no = len(engine.open_positions[symbol].legs) + 1
        lines.append(f"AVERAGING     {symbol:12s} buy {qty} @ ~Rs {price:,.2f}  (leg #{leg_no}, ~Rs {qty * price:,.2f})")
    lines.append(
        f"\nTotal capital already committed to open swing positions: "
        f"Rs {engine.total_capital_committed():,.2f}"
        + (f" (cap: Rs {engine.max_total_capital:,.2f})" if engine.max_total_capital is not None else " (uncapped)")
    )
    summary = "\n".join(lines)

    approved = _confirm_or_abort(summary) if confirm_fn is None else confirm_fn(summary)
    if not approved:
        logger.event("confirmation_declined")
        return

    for symbol, ref_price in pending_entries.items():
        qty = math.floor(CAPITAL_PER_LEG / ref_price)
        if qty < 1:
            logger.event("swing_entry_skipped", symbol=symbol, reason="price exceeds capital_per_leg")
            continue
        try:
            order_id = orders.place_delivery_order(kite, symbol, "BUY", qty, ref_price, tag="swing_entry")
        except orders.OrderRejected as exc:
            logger.event("order_rejected", symbol=symbol, error=str(exc))
            continue
        except orders.OrderPlacementAmbiguous as exc:
            logger.critical(f"{symbol} swing ENTRY order placement is in an UNKNOWN state -- {exc}", symbol=symbol)
            continue
        fill = orders.wait_for_fill(kite, order_id)
        if fill["status"] != "COMPLETE":
            logger.event("swing_entry_not_filled", symbol=symbol, fill_result=fill)
            continue
        engine.open_positions[symbol] = SwingPosition(
            symbol=symbol,
            legs=[(fill["average_price"], fill["filled_quantity"], today)],
            entry_qty=fill["filled_quantity"],
        )
        logger.event("swing_entry", symbol=symbol, qty=fill["filled_quantity"], price=fill["average_price"])
        save_state(engine)

    for symbol, (ref_price, qty, _) in pending_averaging.items():
        try:
            order_id = orders.place_delivery_order(kite, symbol, "BUY", qty, ref_price, tag="swing_averaging")
        except orders.OrderRejected as exc:
            logger.event("order_rejected", symbol=symbol, error=str(exc))
            continue
        except orders.OrderPlacementAmbiguous as exc:
            logger.critical(f"{symbol} swing AVERAGING order placement is in an UNKNOWN state -- {exc}", symbol=symbol)
            continue
        fill = orders.wait_for_fill(kite, order_id)
        if fill["status"] != "COMPLETE":
            logger.event("swing_averaging_not_filled", symbol=symbol, fill_result=fill)
            continue
        engine.open_positions[symbol].legs.append((fill["average_price"], fill["filled_quantity"], today))
        logger.event("swing_averaging", symbol=symbol, qty=fill["filled_quantity"], price=fill["average_price"])
        save_state(engine)

    logger.event("end", new_entries=len(pending_entries), averaging_legs=len(pending_averaging))
