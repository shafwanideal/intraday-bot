"""Read-only JSON endpoint behind the live broker dashboard.

GET /api/portfolio  ->  Kite (Zerodha) + Groww holdings, open positions, funds and
                        P&L, normalised to one shape. Fetched fresh whenever the
                        dashboard page is opened (cached for CACHE_SECONDS so a
                        reload burst doesn't hammer the broker APIs).
GET /               ->  the dashboard page itself (src/dashboard.html). It holds no data and
                        no secrets; it asks for DASHBOARD_TOKEN once, keeps it in the browser,
                        and calls /api/portfolio on every open. Served from here (same origin) on
                        purpose: a claude.ai artifact page is not allowed to fetch from your server.
GET /health         ->  {"ok": true}, no auth, for the reverse proxy.

Read-only by construction: it only calls holdings/positions/margins/quote methods,
never anything that places or modifies an order. Binds to 127.0.0.1 -- put it behind
nginx + HTTPS (deploy/nginx-dashboard-api.conf), then open https://<your-host>/dash/.

Auth: every /api request needs `Authorization: Bearer <DASHBOARD_TOKEN>` (set a long
random value in .env). Without DASHBOARD_TOKEN set the server refuses to start.

Run:  .venv/bin/python -m scripts.run_dashboard_api
"""
import hmac
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

IST = ZoneInfo("Asia/Kolkata")
CACHE_SECONDS = 10
HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
PORT = int(os.getenv("DASHBOARD_PORT", "8765"))

PAGE_FILE = Path(__file__).resolve().parent / "dashboard.html"

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "body": None}
_kite = None
_groww = None


def _num(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _row(broker, kind, symbol, qty, avg, ltp, pnl=None, day_change=None, product=None) -> dict:
    qty, avg, ltp = _num(qty), _num(avg), _num(ltp)
    invested = qty * avg
    value = qty * ltp
    pnl = _num(pnl) if pnl is not None else value - invested
    return {
        "broker": broker,
        "kind": kind,  # holding | position
        "symbol": symbol,
        "product": product,
        "qty": qty,
        "avg": avg,
        "ltp": ltp,
        "invested": invested,
        "value": value,
        "pnl": pnl,
        "pnl_pct": (pnl / invested * 100) if invested else 0.0,
        "day_change": _num(day_change) if day_change is not None else None,
    }


def _kite_client():
    global _kite
    from . import auth

    _kite = auth.refresh_client_if_changed(_kite) if _kite else auth.get_kite()
    return _kite


def _groww_client():
    global _groww
    if _groww is None:
        from .groww_client import get_groww

        _groww = get_groww()
    return _groww


def fetch_kite() -> dict:
    kite = _kite_client()
    rows = []
    for h in kite.holdings():
        qty = _num(h.get("quantity")) + _num(h.get("t1_quantity"))
        ltp = _num(h.get("last_price"))
        rows.append(
            _row("Zerodha", "holding", h["tradingsymbol"], qty, h.get("average_price"), ltp,
                 pnl=h.get("pnl"), day_change=_num(h.get("day_change")) * qty, product=h.get("product")))
    for p in kite.positions().get("net", []):
        if not _num(p.get("quantity")) and not _num(p.get("pnl")):
            continue
        rows.append(
            _row("Zerodha", "position", p["tradingsymbol"], p.get("quantity"), p.get("average_price"),
                 p.get("last_price"), pnl=p.get("pnl"), product=p.get("product")))
    funds = {}
    try:
        eq = kite.margins().get("equity", {})
        funds = {"available": _num(eq.get("available", {}).get("live_balance")),
                 "used": _num(eq.get("utilised", {}).get("debits"))}
    except Exception as exc:  # noqa: BLE001 - funds are a nice-to-have
        funds = {"error": repr(exc)}
    return {"rows": rows, "funds": funds}


def fetch_groww() -> dict:
    g = _groww_client()
    holdings = g.get_holdings_for_user()
    holdings = holdings.get("holdings", []) if isinstance(holdings, dict) else holdings
    positions = g.get_positions_for_user()
    positions = positions.get("positions", []) if isinstance(positions, dict) else positions

    held = [h for h in holdings if _num(h.get("quantity")) > 0]
    open_pos = [p for p in positions if _num(p.get("quantity")) != 0]
    symbols = sorted({h["trading_symbol"] for h in held} | {p["trading_symbol"] for p in open_pos})

    # Groww's holdings carry no live price. Kite's quote feed covers every NSE symbol and is
    # the same feed the bot already trusts; fall back to Groww's own LTP if Kite is down.
    prices: dict[str, float] = {}
    if symbols:
        try:
            q = _kite_client().quote([f"NSE:{s}" for s in symbols])
            prices = {k.split(":", 1)[1]: _num(v.get("last_price")) for k, v in q.items()}
        except Exception:  # noqa: BLE001
            try:
                ltp = g.get_ltp(segment=g.SEGMENT_CASH, exchange_trading_symbols=tuple(f"NSE_{s}" for s in symbols))
                prices = {k.split("_", 1)[1]: _num(v) for k, v in ltp.items()}
            except Exception:  # noqa: BLE001
                prices = {}

    rows = []
    for h in held:
        s = h["trading_symbol"]
        rows.append(_row("Groww", "holding", s, h.get("quantity"), h.get("average_price"), prices.get(s, 0.0),
                         product="CNC"))
    for p in open_pos:
        s = p["trading_symbol"]
        rows.append(_row("Groww", "position", s, p.get("quantity"), p.get("net_price"), prices.get(s, 0.0),
                         product=p.get("product")))
    funds = {}
    try:
        m = g.get_available_margin_details()
        funds = {"available": _num(m.get("clear_cash")), "used": _num(m.get("net_margin_used"))}
    except Exception as exc:  # noqa: BLE001
        funds = {"error": repr(exc)}
    return {"rows": rows, "funds": funds}


def build_snapshot() -> dict:
    out = {"as_of": datetime.now(IST).isoformat(timespec="seconds"), "brokers": {}, "rows": []}
    for name, fn in (("Zerodha", fetch_kite), ("Groww", fetch_groww)):
        try:
            res = fn()
            out["brokers"][name] = {"ok": True, "funds": res["funds"]}
            out["rows"].extend(res["rows"])
        except BaseException as exc:  # noqa: BLE001 - SystemExit from a missing token must not kill the server
            out["brokers"][name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return out


def snapshot_cached() -> dict:
    with _lock:
        if _cache["body"] is None or time.time() - _cache["at"] > CACHE_SECONDS:
            _cache["body"] = build_snapshot()
            _cache["at"] = time.time()
        return _cache["body"]


class Handler(BaseHTTPRequestHandler):
    token = ""

    def _send(self, code: int, body: dict | bytes, content_type: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/health":
            return self._send(200, {"ok": True})
        if path in ("/", "/index.html"):
            return self._send(200, PAGE_FILE.read_bytes(), "text/html; charset=utf-8")
        if path != "/api/portfolio":
            return self._send(404, {"error": "not found"})
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(supplied, self.token):
            return self._send(401, {"error": "bad token"})
        self._send(200, snapshot_cached())

    def log_message(self, fmt, *args):  # keep the journal quiet; no tokens ever reach it
        pass


def serve() -> None:
    token = os.getenv("DASHBOARD_TOKEN", "").strip()
    if len(token) < 24:
        raise SystemExit("Set DASHBOARD_TOKEN in .env to a long random value (24+ chars), "
                         "e.g. `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`.")
    Handler.token = token
    print(f"Dashboard on http://{HOST}:{PORT}/ (read-only; API at /api/portfolio)")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
