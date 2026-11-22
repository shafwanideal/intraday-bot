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

from . import auth, groww_client, ntfy_notify, telegram_notify, yahoo_daily, yahoo_intraday
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

# Market-watch alerts (all holdings, not just the guarded ones). Checked every MONITOR_EVERY_SECONDS.
MOVE_ALERT_PCT = float(os.getenv("GROWW_HOLDINGS_MOVE_PCT", "5"))
VOLUME_MULTS = [float(x) for x in os.getenv("GROWW_HOLDINGS_VOLUME_MULTS", "1.5,2,3").split(",")]
MONITOR_EVERY_SECONDS = 300
ALERTS_FILE = PROJECT_ROOT / "groww_holdings_alerts.json"

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
        """Only: stop hit, sell sent, big move, volume surge, guardian stopped (+ CRITICAL via _notify)."""
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
        elif kind == "holding_big_move":
            arrow = "UP" if f["change_pct"] > 0 else "DOWN"
            self.notifier(f"{sym} is {arrow} {abs(f['change_pct']):.1f}% today: {f['price']:.2f} "
                          f"(prev close {f['prev_close']:.2f}).", title=f"{sym} {f['change_pct']:+.1f}%",
                          priority="high")
        elif kind == "holding_volume_surge":
            self.notifier(f"{sym} volume today is {f['ratio']:.1f}x its 20-day average "
                          f"({f['volume']:,.0f} vs {f['avg_volume']:,.0f}). Price {f['price']:.2f}.",
                          title=f"{sym} volume {f['ratio']:.1f}x")
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


def notify(text: str, title: str = "Holdings guard", priority: str = "default", tags: str = "") -> None:
    """Push to every channel that is configured: Telegram and/or ntfy. Never raises."""
    try:
        if telegram_notify.enabled():
            icon = "\U0001F6A8 " if priority in {"urgent", "high"} else ""
            telegram_notify.send_message(f"{icon}{title}\n{text}")
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: telegram push failed: {exc!r}")
    ntfy_notify.send(text, title=title, priority=priority, tags=tags)


def load_state() -> dict[str, dict]:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state: dict[str, dict]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


_all_symbols: list[str] = []


def fetch_free_holdings(g) -> dict[str, dict] | None:
    """{symbol: {"qty": free qty, "avg": average price}} or None on fetch failure
    (never {} -- an API error must not look like 'you hold nothing')."""
    try:
        resp = g.get_holdings_for_user()
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: could not fetch Groww holdings: {exc!r}")
        return None
    rows = resp.get("holdings", []) if isinstance(resp, dict) else resp
    global _all_symbols
    _all_symbols = sorted({h["trading_symbol"] for h in rows if _num(h.get("quantity")) > 0})
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


def _load_alerts() -> dict:
    try:
        d = json.loads(ALERTS_FILE.read_text())
        return d if d.get("date") == _now().date().isoformat() else {"date": _now().date().isoformat(), "sent": []}
    except (OSError, ValueError):
        return {"date": _now().date().isoformat(), "sent": []}


_avg_vol_cache: dict[str, tuple[str, float]] = {}


def avg_daily_volume(sym: str) -> float:
    """Mean of the last 20 completed sessions' volume (Yahoo daily), cached per day. 0 if unknown."""
    today = _now().date().isoformat()
    hit = _avg_vol_cache.get(sym)
    if hit and hit[0] == today:
        return hit[1]
    try:
        df = yahoo_daily.fetch_daily(sym, days=60, use_cache=False)
        vols = df[df.index.date < _now().date()]["Volume"].dropna().tail(20)
        avg = float(vols.mean()) if len(vols) >= 5 else 0.0
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: avg volume for {sym} failed: {exc!r}")
        avg = 0.0
    _avg_vol_cache[sym] = (today, avg)
    return avg


def fetch_market_stats(symbols: list[str]) -> dict[str, dict]:
    """{sym: {price, prev_close, volume}} -- Kite quotes if available, else Yahoo meta."""
    global _kite
    try:
        _kite = auth.refresh_client_if_changed(_kite) if _kite else auth.get_kite()
        q = _kite.quote([f"NSE:{s}" for s in symbols])
        out = {}
        for k, v in q.items():
            ohlc = v.get("ohlc") or {}
            if _num(v.get("last_price")) > 0 and _num(ohlc.get("close")) > 0:
                out[k.split(":", 1)[1]] = {"price": _num(v["last_price"]), "prev_close": _num(ohlc["close"]),
                                           "volume": _num(v.get("volume") or v.get("volume_traded"))}
        if out:
            return out
    except Exception:  # noqa: BLE001
        _kite = None
    out, now = {}, _now().timestamp()
    for s in symbols:
        try:
            r = yahoo_daily.chart_result(s, {"interval": "5m", "range": "1d"})
        except Exception:  # noqa: BLE001
            continue
        meta = (r or {}).get("meta") or {}
        price, prev = _num(meta.get("regularMarketPrice")), _num(meta.get("chartPreviousClose") or meta.get("previousClose"))
        if price > 0 and prev > 0 and now - _num(meta.get("regularMarketTime")) <= MAX_YAHOO_AGE_MINUTES * 60:
            out[s] = {"price": price, "prev_close": prev, "volume": _num(meta.get("regularMarketVolume"))}
    return out


def market_watch(symbols: list[str], logger: HoldingsLogger) -> None:
    """Alert (once per day per stock/level) on a +-MOVE_ALERT_PCT move from prev close and on volume surges."""
    alerts = _load_alerts()
    sent = set(alerts["sent"])
    for sym, st in fetch_market_stats(symbols).items():
        change = (st["price"] / st["prev_close"] - 1) * 100
        key = f"{sym}:move:{'up' if change > 0 else 'down'}"
        if abs(change) >= MOVE_ALERT_PCT and key not in sent:
            logger.event("holding_big_move", symbol=sym, price=st["price"], prev_close=st["prev_close"],
                         change_pct=round(change, 2))
            sent.add(key)
        avg = avg_daily_volume(sym)
        if avg > 0 and st["volume"] > 0:
            ratio = st["volume"] / avg
            for mult in sorted(VOLUME_MULTS):
                vkey = f"{sym}:vol:{mult:g}"
                if ratio >= mult and vkey not in sent:
                    logger.event("holding_volume_surge", symbol=sym, ratio=ratio, volume=st["volume"],
                                 avg_volume=avg, price=st["price"], level=mult)
                    sent.add(vkey)
                    # one message per poll per stock: mark lower tiers too
                    sent.update(f"{sym}:vol:{m:g}" for m in VOLUME_MULTS if m <= mult)
                    break
    alerts["sent"] = sorted(sent)
    try:
        ALERTS_FILE.write_text(json.dumps(alerts))
    except OSError as exc:
        print(f"WARNING: could not save alerts file: {exc!r}")


_last_watch = 0.0


def check_once(g, state: dict[str, dict], logger: HoldingsLogger) -> bool:
    holdings = fetch_free_holdings(g)
    if holdings is None:
        logger.event("holding_poll_skipped_fetch_failed")
        return False

    global _last_watch
    if _all_symbols and time_module.monotonic() - _last_watch >= MONITOR_EVERY_SECONDS:
        _last_watch = time_module.monotonic()
        try:
            market_watch(_all_symbols, logger)
        except Exception as exc:  # noqa: BLE001 - watch alerts must never break the stops below
            logger.event("market_watch_error", error=repr(exc))

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
                            notifier=notify if (telegram_notify.enabled() or ntfy_notify.enabled()) else None)
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
