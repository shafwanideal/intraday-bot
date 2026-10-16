"""Read-only: which Groww market-data calls does this key allow? Places NO orders."""
from datetime import datetime, timedelta

from src.groww_client import get_groww

g = get_groww()
end = datetime.now()
start = end - timedelta(days=30)
fmt = "%Y-%m-%d %H:%M:%S"
tests = {
    "ltp": lambda: g.get_ltp(segment=g.SEGMENT_CASH, exchange_trading_symbols="NSE_RELIANCE"),
    "ohlc": lambda: g.get_ohlc(segment=g.SEGMENT_CASH, exchange_trading_symbols="NSE_RELIANCE"),
    "daily candles": lambda: g.get_historical_candle_data(
        trading_symbol="RELIANCE", exchange=g.EXCHANGE_NSE, segment=g.SEGMENT_CASH,
        start_time=start.strftime(fmt), end_time=end.strftime(fmt), interval_in_minutes=1440),
    "candles (new api)": lambda: g.get_historical_candles(
        g.EXCHANGE_NSE, g.SEGMENT_CASH, "NSE-RELIANCE",
        start.strftime(fmt), end.strftime(fmt), "1day"),
}
for name, fn in tests.items():
    try:
        out = str(fn())
        print(f"OK     {name}: {out[:100]}")
    except Exception as e:
        print(f"FAILED {name}: {e!r}"[:160])
