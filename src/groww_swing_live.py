"""Groww version of the swing (MTF) trailing-stop guardian (see swing_live.py
for the Zerodha original and the reasoning behind its design).

Exit-only: it never places a BUY. It reads your open MTF positions from
Groww, gets prices from Yahoo (this Groww key has no market-data
permission), arms a trailing stop once price is breakeven% + 2% above your
average, then trails 1.5% below the peak.

ALERT-ONLY by default: with GROWW_LIVE_ORDERS unset/false it only logs
"WOULD SELL" and never touches an order. Real exits need
GROWW_LIVE_ORDERS=true in .env, set by you.

Differences from the Kite version, all deliberate:
- Prices come from Yahoo 1-minute bars, so they can lag. A bar older than
  MAX_BAR_AGE_MINUTES is treated as "no price" and the symbol is skipped
  for that poll rather than acted on.
- Groww has no margin calculator, so the funded (broker-financed) amount
  is estimated as MTF_FUNDED_FRACTION * position value if you set that in
  .env; otherwise the arm point falls back to the flat 2%.
- After sending a real exit it never sends a second one: if the position is
  still there next poll it logs CRITICAL and leaves it for you.
"""

import json
import os
import time as time_module
from datetime import date, datetime

from . import groww_client, yahoo_intraday
from .swing_live import (
    IST,
    LOG_DIR,
    MARKET_CLOSE,
    MARKET_OPEN,
    PROJECT_ROOT,
    SwingLogger,
    _now,
)
from .swing_strategy import PROFIT_TARGET_PCT, compute_mtf_breakeven_pct

STATE_FILE = PROJECT_ROOT / "groww_swing_state.json"
POLL_INTERVAL_SECONDS = 30
TRAIL_PCT = float(os.getenv("GROWW_TRAIL_PCT", "0.015"))
MAX_BAR_AGE_MINUTES = 10
RELOGIN_AFTER_FAILURES = 3
# Verified 2026-10-16 with scripts/test_groww_mtf_sell.py: Groww's API rejects
# product "MTF" ("Invalid product type for CASH segment") and a "CNC" sell fails
# ("You don't have enough shares") because MTF shares are not CNC holdings.
# So the guardian cannot sell MTF positions; it alerts and the user sells in
# the Groww app. Flip this only after re-running the test shows an accepted sell.
API_CAN_SELL_MTF = False
BREACH_POLLS = 2  # consecutive polls at/below the stop before exiting


def _funded_fraction() -> float | None:
    raw = os.getenv("MTF_FUNDED_FRACTION")
    return float(raw) if raw else None


def load_state() -> dict[str, dict]:
    if not STATE_FILE.exists():
        return {}
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state: dict[str, dict]) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def fetch_mtf_positions(g) -> dict[str, dict] | None:
    """{symbol: {"quantity": int, "average_price": float}} for open MTF
    positions, or None on a fetch failure (never {} -- an API error must not
    look like "you hold nothing", which would wipe tracking state)."""
    try:
        resp = g.get_positions_for_user()
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: could not fetch Groww positions: {exc!r}")
        return None
    rows = resp.get("positions", []) if isinstance(resp, dict) else resp
    out = {}
    for p in rows:
        qty = p.get("quantity", 0)
        if p.get("product") == "MTF" and p.get("segment") == "CASH" and qty > 0:
            out[p["trading_symbol"]] = {"quantity": int(qty), "average_price": float(p["net_price"])}
    return out


def fetch_prices(symbols: list[str], logger: SwingLogger) -> dict[str, float]:
    """Latest Yahoo 1-minute close per symbol; stale or missing -> omitted."""
    prices = {}
    now = _now()
    for symbol in symbols:
        try:
            df = yahoo_intraday.fetch_intraday(symbol, period="1d", interval="1m")
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: Yahoo fetch crashed for {symbol}: {exc!r}")
            continue
        if df.empty:
            continue
        age_min = (now - df.index[-1].to_pydatetime()).total_seconds() / 60
        if age_min > MAX_BAR_AGE_MINUTES:
            logger.event("groww_swing_price_stale", symbol=symbol, age_minutes=round(age_min, 1))
            continue
        prices[symbol] = float(df["Close"].iloc[-1])
    return prices


def arm_threshold_pct(pos: dict, st: dict, today: date) -> float:
    if st.get("funded_amount") is None:
        return PROFIT_TARGET_PCT
    days_held = max(0, (today - date.fromisoformat(st["entry_date"])).days)
    value = pos["quantity"] * pos["average_price"]
    return compute_mtf_breakeven_pct(value, st["funded_amount"], days_held) + PROFIT_TARGET_PCT


def _funded_amount(pos: dict) -> float | None:
    frac = _funded_fraction()
    return None if frac is None else frac * pos["quantity"] * pos["average_price"]


def check_once(g, state: dict[str, dict], logger: SwingLogger) -> bool:
    """One poll. Returns False if the positions fetch failed."""
    today = _now().date()
    positions = fetch_mtf_positions(g)
    if positions is None:
        logger.event("groww_swing_poll_skipped_fetch_failed")
        return False

    for symbol in list(state):
        if symbol not in positions:
            logger.event("swing_position_closed_externally", symbol=symbol)
            del state[symbol]

    for symbol, pos in positions.items():
        st = state.get(symbol)
        if st is None:
            state[symbol] = {
                "trailing": False, "peak_price": None, "entry_date": today.isoformat(),
                "funded_amount": _funded_amount(pos), "last_qty": pos["quantity"],
                "exit_sent": False, "alerted": False,
            }
            logger.event("swing_position_detected", symbol=symbol, qty=pos["quantity"], avg_price=pos["average_price"])
        elif pos["quantity"] != st.get("last_qty"):
            st["funded_amount"] = _funded_amount(pos)
            st["last_qty"] = pos["quantity"]

    if not positions:
        return True

    prices = fetch_prices(list(positions), logger)
    for symbol, pos in positions.items():
        ltp = prices.get(symbol)
        if ltp is None:
            continue
        avg, st = pos["average_price"], state[symbol]

        if st.get("exit_sent"):
            if not st.get("exit_warned"):
                logger.critical(f"{symbol}: exit order was sent but the position is still open -- check Groww. "
                                "Not sending another.", symbol=symbol)
                st["exit_warned"] = True
            continue

        if not st["trailing"]:
            arm = arm_threshold_pct(pos, st, today)
            if ltp >= avg * (1 + arm):
                st["trailing"], st["peak_price"] = True, ltp
                logger.event("swing_trailing_armed", symbol=symbol, avg_price=avg, armed_at=ltp, arm_pct=round(arm * 100, 3))
            continue

        st["peak_price"] = max(st["peak_price"], ltp)
        stop = st["peak_price"] * (1 - TRAIL_PCT)
        if ltp > stop:
            st["alerted"] = False
            st["breach_count"] = 0
            continue

        # One bad/laggy Yahoo print must not trigger a sale: require the price
        # to stay at/below the stop for BREACH_POLLS consecutive polls.
        st["breach_count"] = st.get("breach_count", 0) + 1
        if st["breach_count"] < BREACH_POLLS:
            logger.event("groww_swing_breach_pending", symbol=symbol, ltp=ltp, stop=round(stop, 2),
                         count=st["breach_count"])
            continue

        if not (groww_client.live_orders_enabled() and API_CAN_SELL_MTF):
            if not st.get("alerted"):
                logger.event("SELL_NOW_IN_GROWW_APP" if groww_client.live_orders_enabled() else "WOULD_SELL_dry_run", symbol=symbol, qty=pos["quantity"], ltp=ltp,
                             stop=round(stop, 2), peak=st["peak_price"], avg_price=avg)
                st["alerted"] = True
            continue

        try:
            result = groww_client.place_exit_order(g, symbol, pos["quantity"], product="MTF")
        except Exception as exc:  # noqa: BLE001
            logger.critical(f"{symbol} EXIT order failed -- position still open: {exc!r}", symbol=symbol)
            continue
        st["exit_sent"] = True
        logger.event("swing_exit_sent", symbol=symbol, qty=pos["quantity"], ltp=ltp, avg_price=avg, result=result)
    return True


def run_guardian(poll_interval: int = POLL_INTERVAL_SECONDS) -> None:
    g = groww_client.get_groww()
    logger = SwingLogger(LOG_DIR / f"groww_swing_{_now().date().isoformat()}.jsonl")
    state = load_state()
    mode = "LIVE ORDERS" if groww_client.live_orders_enabled() and API_CAN_SELL_MTF else "ALERT-ONLY (Groww API cannot sell MTF -- sell by hand in the app)"
    logger.event("start", mode=mode, tracked=list(state), trail_pct=TRAIL_PCT)
    failures, login_date = 0, _now().date()
    try:
        while True:
            now = _now()
            if not (MARKET_OPEN <= now.time() < MARKET_CLOSE) or now.weekday() >= 5:
                time_module.sleep(min(poll_interval, 60))
                continue
            if now.date() != login_date:  # Groww keys reset daily at 6 AM
                g, login_date = groww_client.get_groww(), now.date()
            try:
                ok = check_once(g, state, logger)
            except Exception as exc:  # noqa: BLE001
                logger.critical(f"check_once() raised -- still polling, investigate: {exc!r}")
                ok = False
            failures = 0 if ok else failures + 1
            if failures >= RELOGIN_AFTER_FAILURES:
                try:
                    g, failures = groww_client.get_groww(), 0
                    logger.event("groww_relogin")
                except BaseException as exc:  # noqa: BLE001 - SystemExit from missing env too
                    logger.critical(f"Groww re-login failed: {exc!r}")
            save_state(state)
            time_module.sleep(poll_interval)
    finally:
        logger.event("end", tracked=list(state))
