"""Trailing stop-loss guardian for Groww DELIVERY (CNC) holdings. Exit-only.

For every holding with FREE (demat) quantity it keeps a peak price, starting at the
price when tracking first sees it, and a stop TRAIL_PCT (default 10%) below that
peak. The peak only moves up. If the price stays at/below the stop for
BREACH_POLLS consecutive polls it fires:
  - ALERT-ONLY (default): phone push + log "STOP HIT -- sell in the Groww app".
  - real CNC market sell of the free quantity, ONLY when BOTH are true:
      API_CAN_SELL_CNC (below) was flipped after scripts.test_groww_cnc_holding_sell
      returned ACCEPTED, AND GROWW_HOLDINGS_LIVE_SELLS=true in .env.
The 10% is measured on your RETURN over your average price by default (see TRAIL_MODE).
It never buys. Holdings with no free quantity (pledged/locked/MTF-style or still
T+1) are not tracked; a T+1 holding starts being tracked once it is delivered.

Prices: Kite real-time quotes when a valid Kite session exists, else Yahoo (can lag
~10 min -- recorded in the log as the price source).
"""

import json
import os
import time as time_module
from datetime import datetime

from . import auth, groww_client, ntfy_notify, yahoo_intraday
from .swing_live import LOG_DIR, MARKET_CLOSE, MARKET_OPEN, PROJECT_ROOT, SwingLogger, _now

STATE_FILE = PROJECT_ROOT / "groww_holdings_state.json"
TRAIL_PCT = float(os.getenv("GROWW_HOLDINGS_TRAIL_PCT", "0.10"))
# "returns" (default): the stop sits TRAIL_PCT of YOUR AVERAGE PRICE below the peak, i.e. it
# trails by 10 percentage points of your return (avg 100, peak 130 -> stop 120).
# "price": the stop sits TRAIL_PCT of the PEAK PRICE below the peak (peak 130 -> stop 117).
TRAIL_MODE = os.getenv("GROWW_HOLDINGS_TRAIL_MODE", "returns").lower()
POLL_INTERVAL_SECONDS = 60
BREACH_POLLS = 2
MAX_YAHOO_AGE_MINUTES = 25
RELOGIN_AFTER_FAILURES = 3

# Flip to True only after the CNC sell test (scripts.test_groww_cnc_holding_sell)
# printed ACCEPTED on a trading day. Until then this guardian only alerts.
API_CAN_SELL_CNC = False


def stop_price(peak: float, avg: float) -> float:
    if TRAIL_MODE == "returns" and avg > 0:
        return max(0.0, peak - TRAIL_PCT * avg)
    return peak * (1 - TRAIL_PCT)


def live_sells_enabled() -> bool:
    return API_CAN_SELL_CNC and os.getenv("GROWW_HOLDINGS_LIVE_SELLS", "").lower() == "true"


class HoldingsLogger(SwingLogger):
    NOTIFY_KINDS: set = set()

    def _push(self, kind: str, f: dict) -> None:
        if self.notifier is None:
            return
        sym = f.get("symbol")
        if kind == "holding_stop_hit":
            self.notifier(
                f"STOP HIT {sym}: price {f['ltp']:.2f} <= stop {f['stop']:.2f} (peak {f['peak']:.2f}, "
                f"{f['gain_pct']:+.1f}% vs your avg). SELL {f['qty']} in the Groww app now.",
                title=f"SELL {sym} NOW", priority="urgent", tags="warning")
        elif kind == "holding_exit_sent":
            self.notifier(f"Sell order sent for {f['qty']} {sym} at ~{f['ltp']:.2f}. Check Groww orders.",
                          title=f"Sold {sym}?", priority="high")
        elif kind == "holding_tracking_started":
            self.notifier(f"Now guarding {sym}: {f['qty']} sh, price {f['ltp']:.2f}, stop {f['stop']:.2f}.",
                          title=f"Guarding {sym}")
        elif kind == "holding_dropped":
            self.notifier(f"{sym} no longer has free quantity; stopped tracking.", title=f"Dropped {sym}")
        elif kind == "start":
            self.notifier(f"Holdings guardian up. {f.get('mode')}. Trail {f.get('trail_pct', 0) * 100:g}% ({f.get('trail_mode')}).",
                          title="Holdings guard up")
        elif kind == "end":
            self.notifier("Holdings guardian STOPPED.", title="Holdings guard down", priority="high", tags="warning")

    def event(self, kind: str, **fields) -> None:
        super().event(kind, **fields)
        try:
            self._push(kind, fields)
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: push formatting failed for {kind}: {exc!r}")

    def _notify(self, text: str) -> None:
        if self.notifier is not None:
            self.notifier(text, title="Holdings guard CRITICAL", priority="urgent", tags="rotating_light")


def load_state() -> dict[str, dict]:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state: dict[str, dict]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def fetch_free_holdings(g) -> dict[str, dict] | None:
    """{symbol: {"qty": free qty, "avg": average price}} or None on fetch failure
    (never {} -- an API error must not look like 'you hold nothing')."""
    try:
        resp = g.get_holdings_for_user()
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: could not fetch Groww holdings: {exc!r}")
        return None
    rows = resp.get("holdings", []) if isinstance(resp, dict) else resp
    out = {}
    for h in rows:
        free = _num(h.get("demat_free_quantity"))
        if free >= 1:
            out[h["trading_symbol"]] = {"qty": int(free), "avg": _num(h.get("average_price"))}
    return out


_kite = None


def fetch_prices(symbols: list[str], logger) -> tuple[dict[str, float], str]:
    """Kite real-time if available, else Yahoo. Returns (prices, source)."""
    global _kite
    try:
        _kite = auth.refresh_client_if_changed(_kite) if _kite else auth.get_kite()
        q = _kite.quote([f"NSE:{s}" for s in symbols])
        prices = {k.split(":", 1)[1]: _num(v.get("last_price")) for k, v in q.items() if _num(v.get("last_price")) > 0}
        if prices:
            return prices, "kite"
    except Exception as exc:  # noqa: BLE001 - no Kite session today is normal; fall back
        _kite = None
        print(f"NOTE: Kite prices unavailable ({exc!s:.100}); using Yahoo")
    prices, now = {}, _now()
    for s in symbols:
        try:
            df = yahoo_intraday.fetch_intraday(s, period="1d", interval="1m")
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: Yahoo fetch crashed for {s}: {exc!r}")
            continue
        if df.empty:
            continue
        age = (now - df.index[-1].to_pydatetime()).total_seconds() / 60
        if age > MAX_YAHOO_AGE_MINUTES:
            logger.event("holding_price_stale", symbol=s, age_minutes=round(age, 1))
            continue
        prices[s] = float(df["Close"].iloc[-1])
    return prices, "yahoo"


def check_once(g, state: dict[str, dict], logger: HoldingsLogger) -> bool:
    holdings = fetch_free_holdings(g)
    if holdings is None:
        logger.event("holding_poll_skipped_fetch_failed")
        return False

    for sym in list(state):
        if sym not in holdings:
            logger.event("holding_dropped", symbol=sym)
            del state[sym]
    if not holdings:
        return True

    prices, source = fetch_prices(list(holdings), logger)
    for sym, h in holdings.items():
        ltp = prices.get(sym)
        if ltp is None:
            continue
        st = state.get(sym)
        if st is None:
            st = state[sym] = {"peak": ltp, "started": _now().isoformat(), "breach": 0,
                               "exit_sent": False, "alerted": False}
            logger.event("holding_tracking_started", symbol=sym, qty=h["qty"], ltp=ltp,
                         stop=round(stop_price(ltp, h["avg"]), 2), source=source)
            continue
        if st["exit_sent"]:
            # Groww may keep listing the shares briefly after a sell; only complain after 15 min.
            waited = (_now() - datetime.fromisoformat(st["exit_time"])).total_seconds() / 60
            if waited > 15 and not st.get("exit_warned"):
                logger.critical(f"{sym}: sell was sent but free quantity is still there -- check Groww. "
                                "Not sending another.", symbol=sym)
                st["exit_warned"] = True
            continue

        st["peak"] = max(st["peak"], ltp)
        stop = stop_price(st["peak"], h["avg"])
        if ltp > stop:
            st["breach"], st["alerted"] = 0, False
            continue
        st["breach"] += 1
        if st["breach"] < BREACH_POLLS:
            logger.event("holding_breach_pending", symbol=sym, ltp=ltp, stop=round(stop, 2), source=source)
            continue

        gain_pct = (ltp / h["avg"] - 1) * 100 if h["avg"] else 0.0
        info = dict(symbol=sym, qty=h["qty"], ltp=ltp, stop=round(stop, 2), peak=st["peak"],
                    avg=h["avg"], gain_pct=gain_pct, source=source)
        if not live_sells_enabled():
            if not st["alerted"]:
                logger.event("holding_stop_hit", **info)
                st["alerted"] = True
            continue
        try:
            result = g.place_order(
                trading_symbol=sym, quantity=h["qty"], validity=g.VALIDITY_DAY, exchange=g.EXCHANGE_NSE,
                segment=g.SEGMENT_CASH, product=g.PRODUCT_CNC, order_type=g.ORDER_TYPE_MARKET,
                transaction_type=g.TRANSACTION_TYPE_SELL)
        except Exception as exc:  # noqa: BLE001
            logger.critical(f"{sym} stop SELL failed: {exc!r}", symbol=sym)
            continue
        status = str(result.get("order_status", "")).upper()
        if not result.get("groww_order_id") or status in {"FAILED", "REJECTED"}:
            logger.critical(f"{sym} stop SELL not accepted: {result}", symbol=sym)
            continue
        st["exit_sent"], st["exit_time"] = True, _now().isoformat()
        logger.event("holding_exit_sent", result=result, **info)
    return True


def run_guardian(poll_interval: int = POLL_INTERVAL_SECONDS) -> None:
    g = groww_client.get_groww()
    logger = HoldingsLogger(LOG_DIR / f"groww_holdings_{_now().date().isoformat()}.jsonl",
                            notifier=ntfy_notify.send if ntfy_notify.enabled() else None)
    state = load_state()
    mode = "LIVE SELLS" if live_sells_enabled() else "ALERT-ONLY (no orders)"
    logger.event("start", mode=mode, trail_pct=TRAIL_PCT, trail_mode=TRAIL_MODE, tracked=list(state))
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
                except BaseException as exc:  # noqa: BLE001
                    logger.critical(f"Groww re-login failed: {exc!r}")
            save_state(state)
            time_module.sleep(poll_interval)
    finally:
        logger.event("end", tracked=list(state))
