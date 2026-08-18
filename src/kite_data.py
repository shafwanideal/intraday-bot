import time
from datetime import datetime, timedelta

import pandas as pd
from kiteconnect.exceptions import KiteException

from . import auth

MARKET_OPEN = "09:15"
MARKET_CLOSE = "15:30"
CHUNK_DAYS = 59  # stay under Kite's per-request window for minute-level candles
REQUEST_DELAY_SECONDS = 0.4  # stay under Kite's ~3 req/sec rate limit

_instrument_cache: dict[str, int] | None = None
_tick_size_cache: dict[str, float] | None = None


def _nse_instruments(kite) -> list[dict]:
    return kite.instruments("NSE")


def _nse_instrument_tokens(kite) -> dict[str, int]:
    global _instrument_cache
    if _instrument_cache is None:
        _instrument_cache = {i["tradingsymbol"]: i["instrument_token"] for i in _nse_instruments(kite)}
    return _instrument_cache


def get_tick_size(kite, symbol: str) -> float:
    """The minimum price increment NSE allows for this symbol's orders --
    typically 0.05, but not guaranteed the same for every instrument. Any
    LIMIT order price must be an exact multiple of this or Kite rejects it."""
    global _tick_size_cache
    if _tick_size_cache is None:
        _tick_size_cache = {i["tradingsymbol"]: i["tick_size"] for i in _nse_instruments(kite)}
    return _tick_size_cache.get(symbol, 0.05)  # 0.05 is the standard NSE equity default


def fetch_intraday(symbol: str, days: int = 180, interval: str = "5minute") -> pd.DataFrame:
    """Fetch intraday OHLC bars for an NSE symbol via Kite's historical data API.

    Requires an authenticated session (src.auth.get_kite()) -- this draws on
    the Kite Connect subscription, unlike src.data's free Yahoo Finance
    fetcher. Chunks requests into <=59-day windows since Kite limits how much
    minute-level data a single request can span, and paginates back `days`
    days, skipping/logging any chunk that errors or returns nothing (e.g. a
    request that lands beyond Kite's retention window) rather than failing
    the whole fetch.
    """
    kite = auth.get_kite()
    tokens = _nse_instrument_tokens(kite)
    token = tokens.get(symbol)
    if token is None:
        print(f"WARNING: {symbol} not found in NSE instrument list, skipping")
        return pd.DataFrame()

    end = datetime.now()
    start = end - timedelta(days=days)

    rows: list[dict] = []
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS), end)
        try:
            candles = kite.historical_data(token, chunk_start, chunk_end, interval)
            rows.extend(candles)
        except KiteException as exc:
            print(f"WARNING: historical fetch failed for {symbol} {chunk_start.date()}-{chunk_end.date()}: {exc}")
        # Chain directly onto chunk_end (not chunk_end + 1 day): a gap here
        # can overshoot past `end` when `days` lands exactly on a multiple of
        # (CHUNK_DAYS + 1), silently dropping the most recent day's data --
        # which includes the default days=180 used throughout this project's
        # backtests. Any duplicate boundary candle this causes is deduped below.
        chunk_start = chunk_end
        time.sleep(REQUEST_DELAY_SECONDS)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.rename(columns={"date": "datetime", "open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
    df["datetime"] = pd.to_datetime(df["datetime"])
    if df["datetime"].dt.tz is None:
        df["datetime"] = df["datetime"].dt.tz_localize("Asia/Kolkata")
    else:
        df["datetime"] = df["datetime"].dt.tz_convert("Asia/Kolkata")
    df = df.set_index("datetime").sort_index()
    df = df[~df.index.duplicated(keep="first")]
    df = df.between_time(MARKET_OPEN, MARKET_CLOSE)
    return df[["Open", "High", "Low", "Close", "Volume"]]


def fetch_many(symbols: list[str], days: int = 180, interval: str = "5minute") -> dict[str, pd.DataFrame]:
    data = {}
    for symbol in symbols:
        df = fetch_intraday(symbol, days=days, interval=interval)
        if df.empty:
            print(f"WARNING: no data returned for {symbol}, skipping")
            continue
        data[symbol] = df
    return data


def fetch_daily(symbol: str, days: int = 45) -> pd.DataFrame:
    """Fetch daily OHLC candles for an NSE symbol, e.g. for ATR calculation."""
    kite = auth.get_kite()
    tokens = _nse_instrument_tokens(kite)
    token = tokens.get(symbol)
    if token is None:
        print(f"WARNING: {symbol} not found in NSE instrument list, skipping")
        return pd.DataFrame()

    end = datetime.now()
    start = end - timedelta(days=days)
    try:
        candles = kite.historical_data(token, start, end, "day")
    except KiteException as exc:
        print(f"WARNING: daily fetch failed for {symbol}: {exc}")
        return pd.DataFrame()

    if not candles:
        return pd.DataFrame()

    df = pd.DataFrame(candles)
    df = df.rename(columns={"date": "datetime", "open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime").sort_index()
    return df[["Open", "High", "Low", "Close", "Volume"]]


def fetch_symbol_atr(symbols: list[str]) -> dict[str, float]:
    """14-day ATR per symbol, for volatility-scaled trailing stops. Skips (with
    a warning) any symbol whose ATR can't be computed."""
    from .indicators import atr as compute_atr

    result = {}
    for symbol in symbols:
        daily = fetch_daily(symbol)
        value = compute_atr(daily) if not daily.empty else None
        if value is not None:
            result[symbol] = value
        else:
            print(f"WARNING: could not compute ATR for {symbol}, it will use the fixed percentage trail instead")
    return result
