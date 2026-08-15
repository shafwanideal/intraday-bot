import pandas as pd
import yfinance as yf

NSE_SUFFIX = ".NS"
MARKET_OPEN = "09:15"
MARKET_CLOSE = "15:30"


def to_yf_symbol(nse_symbol: str) -> str:
    return nse_symbol if nse_symbol.endswith(NSE_SUFFIX) else f"{nse_symbol}{NSE_SUFFIX}"


def fetch_intraday(symbol: str, period: str = "60d", interval: str = "5m") -> pd.DataFrame:
    """Fetch intraday OHLC bars for an NSE symbol via yfinance, restricted to market hours.

    yfinance intraday history is limited by Yahoo: interval "5m" only goes
    back ~60 days, "1m" only ~7 days. This is real recent data, not a
    synthetic/mock series, but it is NOT a multi-year backtest window.
    """
    ticker = yf.Ticker(to_yf_symbol(symbol))
    df = ticker.history(period=period, interval=interval, prepost=False)
    if df.empty:
        return df

    df = df.tz_convert("Asia/Kolkata")
    df = df.between_time(MARKET_OPEN, MARKET_CLOSE)
    df.index.name = "datetime"
    return df[["Open", "High", "Low", "Close", "Volume"]]


def fetch_many(symbols: list[str], period: str = "60d", interval: str = "5m") -> dict[str, pd.DataFrame]:
    data = {}
    for symbol in symbols:
        df = fetch_intraday(symbol, period=period, interval=interval)
        if df.empty:
            print(f"WARNING: no data returned for {symbol}, skipping")
            continue
        data[symbol] = df
    return data
