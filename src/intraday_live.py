"""Live MONITORING (not entry) for MANUALLY-placed intraday (MIS) trades --
the same "detect and protect, never originate" idea as src/swing_live.py,
but for same-day MIS positions instead of multi-day MTF ones.

This is NOT the automated picks engine (src/live.py, run via scripts/daily.py)
-- that already has its own full portfolio-level system (daily loss cap,
profit target, portfolio floor, square-off) for the symbols YOU typed in as
today's picks. This module exists for the OTHER case: an intraday trade you
place on impulse, outside that flow, that would otherwise sit with zero
protection. It applies the same original per-stock trailing-SL rule you
specified for the automated engine (src/strategy.py's
TRAILING_ACTIVATION_PCT / TRAILING_ACTIVATION_PCT_AFTER_NOON /
TRAILING_ACTIVATION_CUTOVER_TIME / trail_pct) to ANY MIS position it finds
that it hasn't seen before -- never places an entry, only ever an exit,
which is what makes this safe to run unattended with no CONFIRM gate, same
argument as swing_live.py and run_live.py's protect_only mode.

IMPORTANT -- do not run this at the same time as scripts/run_live.py is
actively managing the SAME symbol: that would mean two independent
watchers both capable of placing a closing order on the same real
position. To reduce that risk, this module skips any symbol that's a key
in today's TODAYS_STOCKS_FILE (live.py's own plan file) -- those are
presumed to already be run_live.py's responsibility if it's running today.
It only tracks MIS positions outside that plan.

Mechanics, copied faithfully from strategy.GridEngine.update() (see that
docstring for the full reasoning) since reusing GridEngine directly doesn't
fit here -- GridEngine owns portfolio-level daily_pnl/loss-cap/profit-target
state that makes no sense for a single stray manual trade with no "today's
total plan capital" context:
- Arms at TRAILING_ACTIVATION_PCT (1%) before TRAILING_ACTIVATION_CUTOVER_TIME
  (11:00 IST), TRAILING_ACTIVATION_PCT_AFTER_NOON (0.5%) from then on, for a
  position not yet armed. The activation pct a position actually armed at is
  fixed at arm time (armed_activation_pct) -- a later time-based tightening
  never retroactively moves an already-armed position's floor.
- Once armed, trails trail_pct (0.75%, GRID_PCT/2) behind the peak, with a
  lock-in floor at the armed activation level -- the stop can ratchet up
  toward the peak but never back down past the level that locked in the
  original move, same as GridEngine's lock_in_profit=True default.
- Hard square-off at strategy.SQUARE_OFF_TIME (15:08 IST) regardless of
  trailing state -- MIS positions cannot carry overnight, unlike MTF, so
  this is non-negotiable and has no swing_live.py equivalent.

Meant to run CONTINUOUSLY during market hours, same operational shape as
swing_live.py's guardian, via scripts/run_intraday_live.py.
"""

import json
import time as time_module
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from . import auth, config, orders
from .live import TODAYS_STOCKS_FILE
from .strategy import (
    GRID_PCT,
    SQUARE_OFF_TIME,
    TRAILING_ACTIVATION_CUTOVER_TIME,
    TRAILING_ACTIVATION_PCT,
    TRAILING_ACTIVATION_PCT_AFTER_NOON,
    move_pct,
)

IST = ZoneInfo("Asia/Kolkata")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Deliberately separate from swing_positions_state.json -- different product
# (MIS vs MTF), different lifecycle (same-day vs multi-day), must never mix.
INTRADAY_STATE_FILE = PROJECT_ROOT / "intraday_manual_positions_state.json"
LOG_DIR = PROJECT_ROOT / "logs"

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
TRAIL_PCT = GRID_PCT / 2  # 0.75% -- matches live.py's own un-overridden default exactly
POLL_INTERVAL_SECONDS = 5  # faster than swing (intraday moves faster), but see the
# --poll-interval usage note in scripts/run_intraday_live.py about combined rate
# limits if run_live.py's own 1s loop is ALSO polling kite.quote() at the same time.


def _now() -> datetime:
    return datetime.now(IST)


class IntradayLogger:
    """Same jsonl-plus-optional-notifier shape as the other live loggers."""

    NOTIFY_KINDS = {
        "intraday_position_detected",
        "intraday_trailing_armed",
        "intraday_exit",
        "intraday_square_off",
        "intraday_position_closed_externally",
        "start",
        "end",
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
        except Exception as exc:  # noqa: BLE001 - a chat outage must never stop tracking
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
        self._notify(f"⚠️ INTRADAY CRITICAL -- MANUAL ACTION NEEDED\n\n{message}")


def load_state() -> dict[str, dict]:
    """{symbol: {"direction": "long"|"short", "trailing": bool,
    "peak_price": float|None, "armed_activation_pct": float|None,
    "trailing_activated_at": iso-str|None}}. Intentionally reset-worthy day
    to day -- MIS positions never carry overnight, so stale entries here
    from a previous day are meaningless; see run_intraday_guardian, which
    starts fresh each call rather than trusting a leftover file across days."""
    if not INTRADAY_STATE_FILE.exists():
        return {}
    with open(INTRADAY_STATE_FILE) as f:
        return json.load(f)


def save_state(state: dict[str, dict]) -> None:
    with open(INTRADAY_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _todays_plan_symbols() -> set[str]:
    """Symbols already in live.py's own plan file today -- skipped here on
    purpose (see this module's docstring) so this guardian doesn't start
    independently watching/exiting a position run_live.py may already be
    managing under its own portfolio-level rules."""
    if not TODAYS_STOCKS_FILE.exists():
        return set()
    try:
        with open(TODAYS_STOCKS_FILE) as f:
            return set(json.load(f).keys())
    except (json.JSONDecodeError, OSError):
        return set()


def _fetch_mis_positions(kite, skip_symbols: set[str]) -> dict[str, dict] | None:
    """Current real MIS day positions keyed by symbol, excluding anything in
    `skip_symbols` (today's live.py plan). Returns None (NOT {}) on a fetch
    failure -- see swing_live.py's identical fix and its own comment on why
    {} would be dangerous here: indistinguishable from "you hold nothing,"
    which would make check_once() wrongly treat a real open position as
    externally closed and drop it from tracking."""
    try:
        day = kite.positions().get("day", [])
    except orders.PLACEMENT_EXCEPTIONS as exc:
        print(f"WARNING: could not fetch positions from Kite: {exc}")
        return None
    return {
        p["tradingsymbol"]: p
        for p in day
        if p.get("product") == "MIS" and p.get("quantity", 0) != 0 and p["tradingsymbol"] not in skip_symbols
    }


def _fetch_ltp(kite, symbols: list[str]) -> dict[str, float]:
    if not symbols:
        return {}
    try:
        quotes = kite.quote([f"NSE:{s}" for s in symbols])
    except orders.PLACEMENT_EXCEPTIONS as exc:
        print(f"WARNING: could not fetch quotes from Kite: {exc}")
        return {}
    result = {}
    for symbol in symbols:
        q = quotes.get(f"NSE:{symbol}")
        if q and q.get("last_price") is not None:
            result[symbol] = float(q["last_price"])
    return result


def _activation_pct_at(timestamp: datetime) -> float:
    if timestamp.time() >= TRAILING_ACTIVATION_CUTOVER_TIME:
        return TRAILING_ACTIVATION_PCT_AFTER_NOON
    return TRAILING_ACTIVATION_PCT


def _place_exit(kite, symbol: str, direction: str, qty: int, ref_price: float, tag: str, logger: IntradayLogger) -> dict | None:
    """Places the real closing MIS order (SELL for a long, BUY to cover a
    short) and waits for fill confirmation. Returns the fill dict on
    COMPLETE, or None (after logging CRITICAL) on anything else -- caller
    must not update state as if the close succeeded in that case."""
    transaction_type = "SELL" if direction == "long" else "BUY"
    try:
        order_id = orders.place_market_order(kite, symbol, transaction_type, qty, ref_price, tag=tag)
    except orders.OrderRejected as exc:
        logger.critical(
            f"{symbol} intraday {tag} order was REJECTED by Kite -- the real position is almost certainly "
            f"still open: {exc}",
            symbol=symbol,
        )
        return None
    except orders.OrderPlacementAmbiguous as exc:
        logger.critical(f"{symbol} intraday {tag} order placement is in an UNKNOWN state -- {exc}", symbol=symbol)
        return None
    fill = orders.wait_for_fill(kite, order_id)
    if fill["status"] != "COMPLETE":
        logger.critical(
            f"{symbol} intraday {tag} did not confirm filled -- real position may still be open. Check Kite directly.",
            symbol=symbol,
            fill_result=fill,
        )
        return None
    return fill


def check_once(kite, state: dict[str, dict], logger: IntradayLogger) -> None:
    """One pass: reconcile tracked state against Kite's real MIS day
    positions (outside today's live.py plan), apply the trailing-SL rule,
    force square-off past SQUARE_OFF_TIME, and place any real exit that
    triggers. Mutates `state` in place; caller persists it."""
    now = _now()
    skip_symbols = _todays_plan_symbols()
    positions = _fetch_mis_positions(kite, skip_symbols)
    if positions is None:
        logger.event("intraday_poll_skipped_fetch_failed")
        return

    for symbol in list(state.keys()):
        if symbol not in positions:
            logger.event("intraday_position_closed_externally", symbol=symbol)
            del state[symbol]

    for symbol, pos in positions.items():
        if symbol not in state:
            direction = "long" if pos["quantity"] > 0 else "short"
            state[symbol] = {
                "direction": direction,
                "trailing": False,
                "peak_price": None,
                "armed_activation_pct": None,
                "trailing_activated_at": None,
            }
            logger.event(
                "intraday_position_detected", symbol=symbol, direction=direction, qty=abs(pos["quantity"]), avg_price=pos["average_price"]
            )

    if not positions:
        return

    ltps = _fetch_ltp(kite, list(positions.keys()))

    for symbol, pos in positions.items():
        ltp = ltps.get(symbol)
        if ltp is None:
            continue
        st = state[symbol]
        direction = st["direction"]
        avg_price = pos["average_price"]
        qty = abs(pos["quantity"])

        # Hard square-off overrides everything else -- MIS cannot carry overnight.
        if now.time() >= SQUARE_OFF_TIME:
            fill = _place_exit(kite, symbol, direction, qty, ltp, "intraday_square_off", logger)
            if fill is not None:
                logger.event(
                    "intraday_square_off", symbol=symbol, direction=direction, qty=fill["filled_quantity"], price=fill["average_price"]
                )
                del state[symbol]
            continue

        if st["trailing"]:
            activation_pct = st["armed_activation_pct"]
            lock_price = avg_price * (1 + activation_pct) if direction == "long" else avg_price * (1 - activation_pct)
            if direction == "long":
                st["peak_price"] = max(st["peak_price"], ltp)
                trail_level = st["peak_price"] * (1 - TRAIL_PCT)
                stop_level = max(trail_level, lock_price)
                triggered = ltp <= stop_level
            else:
                st["peak_price"] = min(st["peak_price"], ltp)
                trail_level = st["peak_price"] * (1 + TRAIL_PCT)
                stop_level = min(trail_level, lock_price)
                triggered = ltp >= stop_level

            if triggered:
                fill = _place_exit(kite, symbol, direction, qty, ltp, "intraday_exit", logger)
                if fill is not None:
                    logger.event(
                        "intraday_exit",
                        symbol=symbol,
                        direction=direction,
                        qty=fill["filled_quantity"],
                        price=fill["average_price"],
                        avg_price=avg_price,
                    )
                    del state[symbol]
        else:
            move = move_pct(direction, avg_price, ltp)
            arm_threshold = _activation_pct_at(now)
            if move >= arm_threshold:
                st["trailing"] = True
                st["peak_price"] = ltp
                st["armed_activation_pct"] = arm_threshold
                st["trailing_activated_at"] = now.isoformat()
                logger.event(
                    "intraday_trailing_armed", symbol=symbol, direction=direction, avg_price=avg_price, armed_at=ltp, arm_pct=arm_threshold * 100
                )


def run_intraday_guardian(poll_interval: int = POLL_INTERVAL_SECONDS, notifier=None, should_stop=None) -> None:
    """Continuously track manually-placed MIS positions (outside today's
    live.py plan) from market open to close, applying the original
    trailing-SL rule and force-squaring-off by SQUARE_OFF_TIME. No CONFIRM
    gate -- same reasoning as swing_live.py's guardian: this can only ever
    place a protective exit against a position that already exists, never
    commit fresh capital.

    State is NOT loaded from a previous day -- MIS never carries overnight,
    so starting fresh each run is correct, unlike swing_live.py's
    multi-day state file.
    """
    if not config.LIVE_TRADING_ENABLED:
        raise RuntimeError(
            "LIVE_TRADING_ENABLED is not set to 'true' in .env. This is a deliberate safety "
            "gate -- add it explicitly if you actually intend to place real exit orders."
        )

    kite = auth.get_kite()
    today = _now().date()
    logger = IntradayLogger(LOG_DIR / f"intraday_manual_{today.isoformat()}.jsonl", notifier=notifier)
    state: dict[str, dict] = {}

    logger.event("start", tracked=list(state.keys()))

    try:
        while True:
            if should_stop is not None and should_stop():
                logger.event("stop_requested")
                break

            now = _now().time()
            if now < MARKET_OPEN or now >= MARKET_CLOSE:
                time_module.sleep(min(poll_interval, 60))
                continue

            try:
                check_once(kite, state, logger)
            except Exception as exc:  # noqa: BLE001 - see comment below
                # A bug in ONE poll must never silently kill protection on
                # EVERY tracked position for the rest of the day -- found for
                # real 2026-10-14: an order-tag-length ValueError inside
                # check_once propagated all the way out of this loop,
                # crashing the whole process and abandoning both of that
                # day's tracked positions (INDIGO survived only because it
                # hadn't armed yet; ZYDUSLIFE had just armed and correctly
                # decided to exit the instant before the crash hit). Logging
                # CRITICAL and continuing the loop is the same posture
                # run_live.py takes on an exit that can't be confirmed --
                # surface it loudly for a human, but keep watching everything
                # else rather than going dark entirely.
                logger.critical(f"check_once() raised an unexpected exception -- continuing to poll, but investigate now: {exc!r}")
            save_state(state)
            time_module.sleep(poll_interval)
    finally:
        logger.event("end", tracked=list(state.keys()))
