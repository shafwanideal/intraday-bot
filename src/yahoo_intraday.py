"""Free INTRADAY bar fetcher (Yahoo Finance chart API), the login-free
counterpart to `src.kite_data`.

`src.yahoo_daily` does the same job for daily bars and owns the shared
request/retry plumbing (`chart_result`) and the User-Agent that Yahoo's edge
actually serves from a datacenter IP -- read that module's notes before
touching either. This one adds the pieces an intraday backtest needs:
minute-level bars clipped to NSE market hours, and a `fetch_symbol_atr` with
the same contract as `kite_data.fetch_symbol_atr` so it drops straight into
`backtest.run_backtest`.

Why not `src.data`, which already pulls Yahoo intraday bars: that goes
through yfinance, whose curl_cffi TLS impersonation is reset by some
TLS-terminating proxies, and it has no ATR/daily helper.

Two standing limits, both Yahoo's: 5-minute history goes back ~60 days
(1-minute ~7), and the last bar or two of a live session can be missing or
still forming. And this is NOT the feed the bot trades on -- prices come from
a different vendor than Kite's, so expect small differences against
live/shadow mode.
"""

import pandas as pd

from . import yahoo_daily

MARKET_OPEN = "09:15"
MARKET_CLOSE = "15:30"


def fetch_intraday(symbol: str, period: str = "5d", interval: str = "5m") -> pd.DataFrame:
    """Intraday OHLCV for one NSE symbol, restricted to market hours. Returns
    an empty DataFrame (with a warning) rather than raising if the symbol is
    unknown or the fetch fails, matching `kite_data.fetch_intraday`.

    `period` is a Yahoo range token ("5d", "1mo", "60d"), not a day count."""
    result = yahoo_daily.chart_result(symbol, {"interval": interval, "range": period})
    if result is None:
        return pd.DataFrame()

    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    if not timestamps or not quote.get("close"):
        return pd.DataFrame()

    df = pd.DataFrame(
        {
            "Open": quote.get("open"),
            "High": quote.get("high"),
            "Low": quote.get("low"),
            "Close": quote.get("close"),
            "Volume": quote.get("volume"),
        },
        index=pd.to_datetime(timestamps, unit="s", utc=True),
    )
    df.index = df.index.tz_convert("Asia/Kolkata")
    df.index.name = "datetime"
    # Yahoo pads the series with null-OHLC placeholders (halts, and the current
    # bar before it closes); feeding one to the engine would price a fill at NaN.
    # Volume is left alone -- a genuine 0-volume 5-min bar is normal.
    df = df.dropna(subset=["Open", "High", "Low", "Close"]).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    return df.between_time(MARKET_OPEN, MARKET_CLOSE)[["Open", "High", "Low", "Close", "Volume"]]


def fetch_many(symbols: list[str], period: str = "5d", interval: str = "5m") -> dict[str, pd.DataFrame]:
    data = {}
    for symbol in symbols:
        df = fetch_intraday(symbol, period=period, interval=interval)
        if df.empty:
            print(f"WARNING: no intraday data returned for {symbol}, skipping")
            continue
        data[symbol] = df
    return data


def fetch_symbol_atr(symbols: list[str], days: int = 90) -> dict[str, float]:
    """14-day ATR per symbol from daily bars, same contract as
    `kite_data.fetch_symbol_atr`: symbols whose ATR can't be computed are
    skipped with a warning and fall back to the fixed-percentage trail."""
    from .indicators import atr as compute_atr

    result = {}
    for symbol in symbols:
        daily = yahoo_daily.fetch_daily(symbol, days=days)
        value = compute_atr(daily) if not daily.empty else None
        if value is not None:
            result[symbol] = value
        else:
            print(f"WARNING: could not compute ATR for {symbol}, it will use the fixed percentage trail instead")
    return result
