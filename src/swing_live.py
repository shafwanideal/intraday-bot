"""Live MONITORING (not entry) for the swing/positional strategy
(src/swing_strategy.py) on real Zerodha MTF (leveraged) positions.

Entries and averaging legs are placed MANUALLY by the trader, outside this
codebase -- this module never places a BUY. Its only job is to watch
whatever MTF positions actually exist on Kite, apply the same profit-
target-arm / trailing-stop rules as the backtest engine, and place the
matching real SELL the moment a trailing stop is breached.

That one-directional capability is what makes this safe to run
UNATTENDED, continuously, with no CONFIRM gate at all -- same safety
argument as run_live.py's protect_only mode: it can only ever close a
position that's already open, never commit fresh capital. There is
nothing here for a human to approve.

Position discovery and sizing both come straight from Kite
(kite.positions()["net"], product="MTF") on every poll, not from a file
this code maintains:
- The trader's own average_price is always the blended cost Kite itself
  tracks, so a manual averaging leg is picked up automatically on the next
  poll -- no leg bookkeeping needed here at all (contrast with the earlier
  CNC design, which tracked individual legs because it placed the orders
  itself).
- A manual partial or full sell is picked up the same way: this module
  always reads current quantity fresh from Kite at the moment it places an
  exit, never from a cached value.

The only state THIS module needs to persist across polls/restarts is the
trailing-arm flag and peak price per symbol (SWING_STATE_FILE) -- Kite has
no concept of "has this crossed our profit-target-arm threshold yet."

Meant to run CONTINUOUSLY during market hours (see run_swing_guardian),
not once a day -- "constantly track the stocks and book or trail profit,"
per explicit instruction. Uses kite.quote() for live price, polled at
POLL_INTERVAL_SECONDS; swing positions move on the order of minutes to
days, so this does not need run_live.py's 1-second intraday cadence.
"""

import json
import time as time_module
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from . import auth, config, orders
from .swing_strategy import PROFIT_TARGET_PCT, TRAILING_PCT

IST = ZoneInfo("Asia/Kolkata")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SWING_STATE_FILE = PROJECT_ROOT / "swing_positions_state.json"
LOG_DIR = PROJECT_ROOT / "logs"

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
# Swing positions move on the order of minutes to days, not seconds -- no
# need for run_live.py's 1-second intraday cadence (which exists only
# because that endpoint is rate-limited to 1 req/sec and the loop fires
# exactly one call per poll). A moderate interval keeps well under any
# rate limit while still noticing an intraday trailing-stop breach promptly.
POLL_INTERVAL_SECONDS = 30


def _now() -> datetime:
    return datetime.now(IST)


class SwingLogger:
    """Same jsonl-plus-optional-notifier shape as live.py's LiveLogger."""

    NOTIFY_KINDS = {
        "swing_position_detected",
        "swing_trailing_armed",
        "swing_exit",
        "swing_position_closed_externally",
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
        self._notify(f"⚠️ SWING CRITICAL -- MANUAL ACTION NEEDED\n\n{message}")


def load_state() -> dict[str, dict]:
    """{symbol: {"trailing": bool, "peak_price": float|None}}. A missing
    file means nothing is armed yet anywhere -- not an error."""
    if not SWING_STATE_FILE.exists():
        return {}
    with open(SWING_STATE_FILE) as f:
        return json.load(f)


def save_state(state: dict[str, dict]) -> None:
    with open(SWING_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _fetch_mtf_positions(kite) -> dict[str, dict]:
    """Current real MTF positions keyed by symbol, straight from Kite --
    the only source of truth for what's actually held and at what average
    price. Returns {} on a fetch failure rather than raising, so a
    transient API hiccup doesn't kill the whole poll loop."""
    try:
        net = kite.positions().get("net", [])
    except orders.PLACEMENT_EXCEPTIONS as exc:
        print(f"WARNING: could not fetch positions from Kite: {exc}")
        return {}
    return {p["tradingsymbol"]: p for p in net if p.get("product") == orders.PRODUCT_MTF and p.get("quantity", 0) > 0}


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


def check_once(kite, state: dict[str, dict], logger: SwingLogger) -> None:
    """One pass: reconcile tracked state against Kite's real MTF positions,
    then apply profit-target-arm / trailing-stop logic and place any real
    exit that triggers. Mutates `state` in place; caller is responsible for
    persisting it (see run_swing_guardian)."""
    positions = _fetch_mtf_positions(kite)

    for symbol in list(state.keys()):
        if symbol not in positions:
            logger.event("swing_position_closed_externally", symbol=symbol)
            del state[symbol]

    for symbol, pos in positions.items():
        if symbol not in state:
            state[symbol] = {"trailing": False, "peak_price": None}
            logger.event(
                "swing_position_detected", symbol=symbol, qty=pos["quantity"], avg_price=pos["average_price"]
            )

    if not positions:
        return

    ltps = _fetch_ltp(kite, list(positions.keys()))

    for symbol, pos in positions.items():
        ltp = ltps.get(symbol)
        if ltp is None:
            continue
        avg_price = pos["average_price"]
        st = state[symbol]

        if st["trailing"]:
            st["peak_price"] = max(st["peak_price"], ltp)
            stop_level = st["peak_price"] * (1 - TRAILING_PCT)
            if ltp > stop_level:
                continue

            qty = positions[symbol]["quantity"]  # re-read fresh, never cached, in case of a manual partial sell
            try:
                order_id = orders.place_mtf_order(kite, symbol, "SELL", qty, ltp, tag="swing_exit")
            except orders.OrderRejected as exc:
                logger.critical(
                    f"{symbol} swing EXIT order was REJECTED by Kite -- the real position is almost certainly "
                    f"still open: {exc}",
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
                    "Check Kite directly.",
                    symbol=symbol,
                    fill_result=fill,
                )
                continue

            fill_price = fill["average_price"] or ltp
            filled_qty = fill["filled_quantity"] or qty
            gross_pnl = (fill_price - avg_price) * filled_qty
            logger.event(
                "swing_exit",
                symbol=symbol,
                qty=filled_qty,
                price=fill_price,
                avg_price=avg_price,
                gross_pnl=round(gross_pnl, 2),
                note="gross only -- excludes MTF daily funding interest and entry-side charges, which this "
                "module never saw (entry was manual); see Kite's own ledger for the true net figure",
            )
            del state[symbol]
        else:
            if ltp >= avg_price * (1 + PROFIT_TARGET_PCT):
                st["trailing"] = True
                st["peak_price"] = ltp
                logger.event("swing_trailing_armed", symbol=symbol, avg_price=avg_price, armed_at=ltp)


def run_swing_guardian(poll_interval: int = POLL_INTERVAL_SECONDS, notifier=None, should_stop=None) -> None:
    """Continuously track real MTF positions from market open to close,
    auto-exiting on a trailing-stop breach. No CONFIRM gate -- see this
    module's own docstring for why that's safe here specifically (exit-only,
    never a new entry).

    should_stop() -> bool, polled each cycle, same panic-button contract as
    run_live.py's should_stop: ends the loop, does not place any order on
    its own (an in-progress trailing stop already breached still fires --
    this is a monitoring stop, not a square-off).
    """
    if not config.LIVE_TRADING_ENABLED:
        raise RuntimeError(
            "LIVE_TRADING_ENABLED is not set to 'true' in .env. This is a deliberate safety "
            "gate -- add it explicitly if you actually intend to place real exit orders."
        )

    kite = auth.get_kite()
    today = _now().date()
    logger = SwingLogger(LOG_DIR / f"swing_{today.isoformat()}.jsonl", notifier=notifier)
    state = load_state()

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

            check_once(kite, state, logger)
            save_state(state)
            time_module.sleep(poll_interval)
    finally:
        logger.event("end", tracked=list(state.keys()))
