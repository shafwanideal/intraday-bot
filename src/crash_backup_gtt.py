"""A third, fully separate, isolated add-on: a DAILY backup stop-loss GTT
for every MTF holding that Part 1 (the exit-only guardian) manages by
default -- i.e. anything with NO existing active GTT from anywhere else.

Why this exists (explicit request, 2026-11-25): Part 1's own "crash
stop-loss" (-8%/-5% from yesterday's close, see swing_live.py's
CRASH_STOP_LOSS_PCT/NIFTY50_CRASH_STOP_LOSS_PCT) is NOT a resting
broker-side order -- it's the Python process noticing the drop and placing
a market sell every poll. If the VPS or the process itself is down during
a crash, that layer simply isn't watching. This module adds a genuine
broker-side safety net that survives the bot process being down: a
SINGLE-LEG stop-loss GTT at the same -8%/-5% threshold, refreshed once
every morning (the threshold moves every day, since it's based on
yesterday's close).

Why single-leg, specifically: Part 1's own fetch_manual_gtt_info (in
swing_live.py) only ever treats a TWO-LEG (OCO) GTT as "manually managed,
stand down" -- a single-leg GTT is invisible to that check. That's what
keeps this module from disturbing Part 1 at all: Part 1 keeps trailing and
crash-checking a stock exactly as before, with this GTT sitting underneath
as pure backup. Confirmed scope (explicit instruction): only stocks with
NO existing active GTT from ANYWHERE (not Part 2's picks, not an
auto-adopted stock, not the trader's own manual GTT) get this backup --
anything already GTT-managed already has its own resting stop serving the
same purpose, so adding a second one here would be redundant.

Isolation, same promise as pdf_entry_live.py: own state file
(crash_backup_gtt_state.json), own log file, own tmux session (NOT merged
into run_all_guardians.py or pdf_entry's process), nothing imported from
swing_live.py/intraday_live.py/hourly_pnl.py/digest.py/alerts.py -- NIFTY_
50_SYMBOLS, the tick-size helper, and the crash thresholds are all
duplicated locally rather than shared, matching how pdf_entry_live.py
already duplicates rather than imports (e.g. its own _funded_amount_for_
auto instead of importing swing_live.py's _funded_amount_for_position).

GTT response-shape and tick-size assumptions are the same as
pdf_entry_live.py's (see that module's own docstring) -- not re-verified
separately here, but this module uses the identical _place_single_leg_sl/
_get_tick_size pattern, already confirmed working live 2026-11-23.
"""

import json
import os
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from . import auth, heartbeat
from .orders import PLACEMENT_EXCEPTIONS, PRODUCT_MTF

# Same separate-gate philosophy as pdf_entry_live.py's PDF_ENTRY_TRADING_ENABLED
# -- this places real stop-loss GTTs with real money behind them, so it gets
# its own explicit opt-in rather than silently riding on another flag.
CRASH_BACKUP_GTT_ENABLED = os.environ.get("CRASH_BACKUP_GTT_ENABLED", "").strip().lower() == "true"

IST = ZoneInfo("Asia/Kolkata")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
STATE_FILE = LOG_DIR / "crash_backup_gtt_state.json"

# Just after the market opens (9:15) -- gives the day's opening quote/prev_close
# a moment to settle, and avoids NSE's pre-open-only-limit-orders window.
RUN_TIME = time(9, 16)
CHECK_INTERVAL_SECONDS = 300  # 5 minutes -- this only needs to notice "it's a new day past 9:16" once

# Identical values to swing_live.py's own CRASH_STOP_LOSS_PCT/NIFTY50_CRASH_
# STOP_LOSS_PCT -- duplicated, not imported, per this module's own isolation
# promise (see module docstring).
CRASH_STOP_LOSS_PCT = 0.08
NIFTY50_CRASH_STOP_LOSS_PCT = 0.05

# Duplicated from swing_live.py's own NIFTY_50_SYMBOLS -- same staleness
# caveat applies (Nifty50 rebalances twice a year); kept here rather than
# imported so this module has zero dependency on swing_live.py.
NIFTY_50_SYMBOLS = {
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "BHARTIARTL", "ITC", "SBIN",
    "HINDUNILVR", "LT", "BAJFINANCE", "KOTAKBANK", "AXISBANK", "ASIANPAINT", "MARUTI",
    "SUNPHARMA", "TITAN", "ULTRACEMCO", "NESTLEIND", "WIPRO", "M&M", "NTPC",
    "POWERGRID", "ONGC", "TATAMOTORS", "TATASTEEL", "ADANIENT", "ADANIPORTS",
    "JSWSTEEL", "COALINDIA", "BAJAJFINSV", "HCLTECH", "TECHM", "INDUSINDBK",
    "DRREDDY", "CIPLA", "DIVISLAB", "EICHERMOT", "GRASIM", "HEROMOTOCO",
    "BRITANNIA", "BPCL", "SBILIFE", "HDFCLIFE", "APOLLOHOSP", "TATACONSUM",
    "BAJAJ-AUTO", "SHRIRAMFIN", "LTIM", "HINDALCO", "TRENT",
}

GTT_FILL_BUFFER_PCT = 0.003
TICK_SIZE = 0.05
_INSTRUMENT_TICK_CACHE: dict[str, float] = {}


def _now() -> datetime:
    return datetime.now(IST)


def _get_tick_size(kite, symbol: str) -> float:
    """Same real-per-instrument lookup as pdf_entry_live.py's own
    _get_tick_size (NSE tick size is NOT a universal 0.05 -- confirmed live
    2026-11-23). Duplicated, not imported, per this module's isolation."""
    if symbol in _INSTRUMENT_TICK_CACHE:
        return _INSTRUMENT_TICK_CACHE[symbol]
    try:
        for inst in kite.instruments("NSE"):
            sym = inst.get("tradingsymbol")
            tick = inst.get("tick_size")
            if sym and tick:
                _INSTRUMENT_TICK_CACHE[sym] = float(tick)
    except PLACEMENT_EXCEPTIONS + (KeyError, TypeError, ValueError):
        pass
    return _INSTRUMENT_TICK_CACHE.get(symbol, TICK_SIZE)


def _round_to_tick(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 2)


class CrashBackupLogger:
    NOTIFY_KINDS = {"crash_backup_gtt_placed", "crash_backup_gtt_error", "crash_backup_gtt_skipped_other_gtt"}

    def __init__(self, log_path: Path, notifier=None):
        self.log_path = log_path
        self.notifier = notifier
        log_path.parent.mkdir(exist_ok=True)

    def _notify(self, text: str) -> None:
        if self.notifier is None:
            return
        try:
            self.notifier(text)
        except Exception as exc:  # noqa: BLE001 - a chat outage must never stop the refresh
            print(f"WARNING: notifier failed: {exc}")

    def event(self, kind: str, **fields) -> None:
        record = {"timestamp": _now().isoformat(), "kind": kind, **fields}
        print(f"[{record['timestamp']}] {kind}: {fields}")
        with open(self.log_path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
        if kind in self.NOTIFY_KINDS:
            self._notify(_format_event(kind, fields))


def _format_event(kind: str, fields: dict) -> str:
    sym = fields.get("symbol", "?")
    if kind == "crash_backup_gtt_placed":
        return (f"🛡️ {sym}: daily backup stop-loss refreshed at ₹{fields['threshold']:.2f} "
                f"({fields['pct']:.0f}% below today's prev close ₹{fields['prev_close']:.2f})")
    if kind == "crash_backup_gtt_skipped_other_gtt":
        return f"ℹ️ {sym}: already has its own GTT (Part 2 or manual) -- no backup needed, skipped"
    if kind == "crash_backup_gtt_error":
        return f"⚠️ {sym}: {fields.get('message', 'error')}"
    return f"{kind}: {fields}"


def load_state() -> dict[str, dict]:
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict[str, dict]) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    tmp.replace(STATE_FILE)


def _place_single_leg_sl(kite, symbol: str, qty: int, threshold: float, ltp: float) -> int:
    tick = _get_tick_size(kite, symbol)
    threshold = _round_to_tick(threshold, tick)
    limit_price = _round_to_tick(threshold * (1 - GTT_FILL_BUFFER_PCT), tick)
    resp = kite.place_gtt(
        trigger_type=kite.GTT_TYPE_SINGLE,
        tradingsymbol=symbol,
        exchange=kite.EXCHANGE_NSE,
        trigger_values=[threshold],
        last_price=ltp,
        orders=[
            {"transaction_type": "SELL", "quantity": qty, "order_type": kite.ORDER_TYPE_LIMIT, "product": PRODUCT_MTF, "price": limit_price},
        ],
    )
    return resp["trigger_id"]


def refresh_crash_backup_gtts(kite, state: dict[str, dict], logger: CrashBackupLogger) -> None:
    """The once-a-day job. For every current MTF holding:
      - if it already has an active GTT that ISN'T one we placed ourselves
        (tracked via `state`) -- Part 2 or the trader's own GTT -- skip it,
        per the confirmed scope ("only default Part 1 stocks").
      - otherwise (no GTT at all, or an active GTT matching our own stored
        trigger_id from a PREVIOUS day) -- cancel any stale one of ours and
        place a fresh single-leg stop-loss at today's -8%/-5% threshold.
    Also cleans up state for any symbol no longer held."""
    try:
        positions = kite.positions().get("net", [])
    except PLACEMENT_EXCEPTIONS:
        return
    held = {p["tradingsymbol"]: p for p in positions if p.get("product") == PRODUCT_MTF and (p.get("quantity") or 0) > 0}

    try:
        all_gtts = kite.get_gtts()
    except PLACEMENT_EXCEPTIONS:
        return
    active_gtt_by_symbol: dict[str, int] = {}
    for g in all_gtts:
        if g.get("status") != "active":
            continue
        sym = (g.get("condition") or {}).get("tradingsymbol")
        if sym:
            active_gtt_by_symbol[sym] = g.get("id")

    # Stale-state cleanup runs regardless of whether anything is currently
    # held -- selling out of EVERY MTF position must not leave yesterday's
    # backup GTTs (or their state entries) behind forever.
    for symbol in list(state.keys()):
        if symbol not in held:
            stale_gtt_id = state[symbol].get("trigger_id")
            if stale_gtt_id is not None and active_gtt_by_symbol.get(symbol) == stale_gtt_id:
                try:
                    kite.delete_gtt(stale_gtt_id)
                except PLACEMENT_EXCEPTIONS:
                    pass
            state.pop(symbol)

    if not held:
        save_state(state)
        return

    try:
        quotes = kite.quote([f"NSE:{s}" for s in held])
    except PLACEMENT_EXCEPTIONS:
        save_state(state)
        return

    today_str = _now().date().isoformat()

    for symbol, pos in held.items():
        active_gtt_id = active_gtt_by_symbol.get(symbol)
        our_prior_gtt_id = state.get(symbol, {}).get("trigger_id")

        if active_gtt_id is not None and active_gtt_id != our_prior_gtt_id:
            # Someone else's GTT (Part 2's pick/auto-adopt, or the trader's
            # own) -- already has its own resting stop-loss, leave it alone.
            logger.event("crash_backup_gtt_skipped_other_gtt", symbol=symbol)
            state.pop(symbol, None)
            continue

        if state.get(symbol, {}).get("date") == today_str:
            continue  # already refreshed today

        quote = quotes.get(f"NSE:{symbol}")
        if quote is None or quote.get("last_price") is None:
            continue
        ltp = float(quote["last_price"])
        prev_close = (quote.get("ohlc") or {}).get("close")
        if not prev_close:
            continue
        prev_close = float(prev_close)

        pct = NIFTY50_CRASH_STOP_LOSS_PCT if symbol in NIFTY_50_SYMBOLS else CRASH_STOP_LOSS_PCT
        threshold = prev_close * (1 - pct)
        qty = pos["quantity"]

        if active_gtt_id is not None:  # our own, from a previous day -- stale threshold, replace it
            try:
                kite.delete_gtt(active_gtt_id)
            except PLACEMENT_EXCEPTIONS:
                pass

        try:
            trigger_id = _place_single_leg_sl(kite, symbol, qty, threshold, ltp)
        except PLACEMENT_EXCEPTIONS as exc:
            logger.event("crash_backup_gtt_error", symbol=symbol, message=f"could not place/refresh backup stop-loss: {exc}")
            continue

        state[symbol] = {"trigger_id": trigger_id, "date": today_str, "threshold": threshold}
        logger.event("crash_backup_gtt_placed", symbol=symbol, threshold=threshold, pct=pct * 100, prev_close=prev_close)

    save_state(state)


def run_crash_backup_gtt(notifier=None, should_stop=None) -> None:
    """Runs forever as its own process (own tmux session -- see the runner
    script), checking every CHECK_INTERVAL_SECONDS whether it's a new day
    past RUN_TIME and the daily refresh hasn't happened yet."""
    import time as time_module

    if not CRASH_BACKUP_GTT_ENABLED:
        raise RuntimeError(
            "CRASH_BACKUP_GTT_ENABLED is not set to 'true' in .env. This is a deliberate "
            "separate safety gate for the daily backup stop-loss GTT -- add it explicitly "
            "if you actually intend this to place real stop-loss GTTs."
        )

    kite = auth.get_kite()
    today = _now().date()
    logger = CrashBackupLogger(LOG_DIR / f"crash_backup_gtt_{today.isoformat()}.jsonl", notifier=notifier)
    state = load_state()
    last_refreshed_date: date | None = None
    logger.event("start", tracked=list(state.keys()))

    while True:
        if should_stop is not None and should_stop():
            logger.event("end", tracked=list(state.keys()))
            return

        heartbeat.touch("crash_backup_gtt")

        now = _now()
        if now.date() != today:
            today = now.date()
            logger.log_path = LOG_DIR / f"crash_backup_gtt_{today.isoformat()}.jsonl"

        fresh = auth.refresh_client_if_changed(kite)
        if fresh is not kite:
            kite = fresh
            logger.event("kite_token_refreshed")

        if now.date() != last_refreshed_date and now.time() >= RUN_TIME:
            try:
                refresh_crash_backup_gtts(kite, state, logger)
                last_refreshed_date = now.date()
            except Exception as exc:  # noqa: BLE001 - one bad day must never kill the process
                logger.event("crash_backup_gtt_error", symbol="ALL", message=f"refresh failed: {exc!r}")

        time_module.sleep(CHECK_INTERVAL_SECONDS)
