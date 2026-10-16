"""Thin wrapper over Groww's official Trade API (`growwapi` SDK).

Auth: set GROWW_API_KEY + GROWW_API_SECRET in .env (key and secret from
Groww's API keys page). Alternatively set GROWW_ACCESS_TOKEN directly.
Orders are dry-run unless GROWW_LIVE_ORDERS=true.

NOTE: written against the growwapi SDK docs from memory and not yet run
against a live account -- run scripts/groww_check.py first and fix any
method-name drift it reports.
"""
import os

from dotenv import load_dotenv

load_dotenv()


def get_groww():
    from growwapi import GrowwAPI

    token = os.getenv("GROWW_ACCESS_TOKEN")
    if not token:
        key, secret = os.getenv("GROWW_API_KEY"), os.getenv("GROWW_API_SECRET")
        if not key or not secret:
            raise SystemExit("Set GROWW_API_KEY and GROWW_API_SECRET in .env")
        token = GrowwAPI.get_access_token(api_key=key, secret=secret)
    return GrowwAPI(token)


def live_orders_enabled() -> bool:
    return os.getenv("GROWW_LIVE_ORDERS", "").lower() == "true"


def place_delivery_order(g, symbol: str, qty: int, side: str, limit_price: float | None = None):
    """Place a CNC (delivery) NSE cash order. side: 'BUY' or 'SELL'."""
    args = dict(
        trading_symbol=symbol,
        quantity=qty,
        validity=g.VALIDITY_DAY,
        exchange=g.EXCHANGE_NSE,
        segment=g.SEGMENT_CASH,
        product=g.PRODUCT_CNC,
        order_type=g.ORDER_TYPE_LIMIT if limit_price else g.ORDER_TYPE_MARKET,
        transaction_type=g.TRANSACTION_TYPE_BUY if side == "BUY" else g.TRANSACTION_TYPE_SELL,
    )
    if limit_price:
        args["price"] = limit_price
    if not live_orders_enabled():
        print(f"[DRY RUN] would place: {args}")
        return None
    return g.place_order(**args)
