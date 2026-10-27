import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from kiteconnect.exceptions import KiteException

from . import auth

MARKET_OPEN = "09:15"
MARKET_CLOSE = "15:30"
CHUNK_DAYS = 59  # stay under Kite's per-request window for minute-level candles
REQUEST_DELAY_SECONDS = 0.4  # stay under Kite's ~3 req/sec rate limit

_instrument_cache: dict[str, int] | None = None

CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "kite_daily"


def _nse_instruments(kite) -> list[dict]:
    return kite.instruments("NSE")


def _nse_instrument_tokens(kite) -> dict[str, int]:
    global _instrument_cache
    if _instrument_cache is None:
        _instrument_cache = {i["tradingsymbol"]: i["instrument_token"] for i in _nse_instruments(kite)}
    return _instrument_cache


def fetch_intraday(symbol: str, days: int = 180, interval: str = "5minute") -> pd.DataFrame:
    """Fetch intraday OHLC bars for an NSE symbol via Kite's historical data API.

    Requires an authenticated session (src.auth.get_kite()) -- this draws on
    the Kite Connect subscription. Chunks requests into <=59-day windows since Kite limits how much
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


def _coerce(df: pd.DataFrame) -> pd.DataFrame:
    """One canonical dtype/index shape for the network and cache paths, so a cached
    run and an uncached run of the same backtest see identical frames (the JSON
    round-trip otherwise turns int Volume into float and can change the index unit)."""
    df = df.astype({"Open": "float64", "High": "float64", "Low": "float64", "Close": "float64", "Volume": "float64"})
    df.index = pd.DatetimeIndex(df.index).as_unit("ns")
    df.index.name = "datetime"
    return df


def _cache_path(symbol: str, days: int) -> Path:
    return CACHE_DIR / f"{symbol}_{days}d_{date.today().isoformat()}.json"


def _read_cache(symbol: str, days: int) -> pd.DataFrame | None:
    path = _cache_path(symbol, days)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
        df = pd.DataFrame(payload["rows"], columns=["Open", "High", "Low", "Close", "Volume"], index=pd.to_datetime(payload["index"]))
    except (json.JSONDecodeError, OSError, KeyError, ValueError):
        return None  # a truncated/corrupt cache file just means "re-fetch"
    return _coerce(df)


def _write_cache(symbol: str, days: int, df: pd.DataFrame) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(symbol, days).write_text(
            json.dumps({"index": [d.isoformat() for d in df.index], "rows": df.values.tolist()})
        )
    except OSError as exc:
        print(f"WARNING: could not cache {symbol} ({exc}) -- continuing without caching")


def fetch_daily_cached(symbol: str, days: int = 850, use_cache: bool = True) -> pd.DataFrame:
    """fetch_daily() with a per-day on-disk cache, so re-running a universe-wide
    backtest with different strategy parameters doesn't re-download every symbol.
    The cache is keyed by today's date, so it never serves yesterday's bars."""
    if use_cache:
        cached = _read_cache(symbol, days)
        if cached is not None:
            return cached
    df = fetch_daily(symbol, days=days)
    if df.empty:
        return df
    df = _coerce(df)
    if use_cache:
        _write_cache(symbol, days, df)
    return df


def fetch_daily_many(symbols: list[str], days: int = 850, progress_every: int = 25, use_cache: bool = True) -> dict[str, pd.DataFrame]:
    """Daily bars for a whole universe, skipping symbols that fail."""
    data: dict[str, pd.DataFrame] = {}
    fetched = 0
    for i, symbol in enumerate(symbols, start=1):
        cached = _read_cache(symbol, days) if use_cache else None
        if cached is not None:
            data[symbol] = cached
        else:
            df = fetch_daily_cached(symbol, days=days, use_cache=use_cache)
            fetched += 1
            if not df.empty:
                data[symbol] = df
            time.sleep(REQUEST_DELAY_SECONDS)  # only after a real network call
        if progress_every and i % progress_every == 0:
            print(f"  ... {i}/{len(symbols)} symbols ({len(data)} with data, {fetched} fetched, {i - fetched} from cache)")
    return data


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
