"""Read-only Groww connectivity check. Places NO orders."""
from src.groww_client import get_groww

g = get_groww()
print("Logged in.")
for label, fn in [
    ("Margin", lambda: g.get_available_margin_details()),
    ("Holdings", lambda: g.get_holdings_for_user()),
    ("Positions", lambda: g.get_positions_for_user()),
    ("RELIANCE quote", lambda: g.get_quote(trading_symbol="RELIANCE", exchange=g.EXCHANGE_NSE, segment=g.SEGMENT_CASH)),
]:
    try:
        print(f"\n== {label} ==\n{fn()}")
    except Exception as e:
        print(f"\n== {label} FAILED: {e!r}")
