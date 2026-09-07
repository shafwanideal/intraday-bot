"""Free daily-OHLC fetcher (Yahoo Finance chart API) for the swing backtests.

Why this exists alongside `src.kite_data.fetch_daily`: the swing/entry-trigger
backtests (`scripts/run_52w_low_backtest.py`) need ~850 days of *daily* bars
for a few hundred symbols and nothing else -- no intraday granularity. Kite
can serve that, but only behind a same-day interactive login, which means
those backtests can't run in any non-interactive environment (CI, a sandbox,
anywhere without a fresh access token). Daily bars are free and unauthenticated
from Yahoo, so this makes the entry-trigger comparison runnable anywhere.

It deliberately does NOT use the `yfinance` package (already a dependency, used
by `src.data` for intraday bars): yfinance routes through `curl_cffi`, which
impersonates a browser's TLS fingerprint and fails behind a TLS-terminating
corporate/agent proxy. Plain `requests` against the same public endpoint works
in both environments.

Prices are Yahoo's `quote` OHLC, which is SPLIT-adjusted but not
dividend-adjusted -- the right basis for a price-level trigger like "fresh
52-week low", since those are the levels the stock actually traded at. Do not
substitute `adjclose` here: back-adjusting for dividends shifts historical
lows to prices that never printed.
"""

import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
NSE_SUFFIX = ".NS"
# Yahoo's edge throttles by User-Agent, and counter-intuitively the bare token
# below is the one that gets through from a datacenter IP: both the default
# python-requests UA and a full, realistic Chrome UA string were served a
# blanket "Edge: Too Many Requests" 429 on every single request, while this
# returned 200 on every one. Do not "improve" this into a realistic browser UA.
HEADERS = {"User-Agent": "Mozilla/5.0"}
REQUEST_DELAY_SECONDS = 0.3  # be a good citizen; Yahoo rate-limits shared egress IPs hard
MAX_RETRIES = 4


def to_yf_symbol(nse_symbol: str) -> str:
    return nse_symbol if nse_symbol.endswith(NSE_SUFFIX) else f"{nse_symbol}{NSE_SUFFIX}"


def fetch_daily(symbol: str, days: int = 850) -> pd.DataFrame:
    """Daily OHLCV for one NSE symbol. Returns an empty DataFrame (with a
    warning) rather than raising if the symbol is unknown or the fetch fails,
    matching `src.kite_data.fetch_daily`'s contract -- a universe-wide backtest
    should skip a bad symbol, not die on it."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    params = {
        "period1": int(start.timestamp()),
        "period2": int(end.timestamp()),
        "interval": "1d",
        "events": "split",
    }

    last_error: str | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(CHART_URL.format(symbol=to_yf_symbol(symbol)), params=params, headers=HEADERS, timeout=30)
            if resp.status_code == 429 or resp.status_code >= 500:
                # Shared-IP rate limiting is the common failure here, and it clears
                # on its own -- back off rather than dropping the symbol.
                last_error = f"HTTP {resp.status_code}"
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = str(exc)
            time.sleep(2 ** attempt)
            continue

        result = (payload.get("chart") or {}).get("result")
        if not result:
            error = (payload.get("chart") or {}).get("error")
            print(f"WARNING: no data for {symbol} from Yahoo ({error}), skipping")
            return pd.DataFrame()
        return _to_frame(result[0])

    print(f"WARNING: Yahoo fetch failed for {symbol} after {MAX_RETRIES} attempts ({last_error}), skipping")
    return pd.DataFrame()


def _to_frame(result: dict) -> pd.DataFrame:
    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators") or {}).get("quote") or [{}]
    quote = quote[0]
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
    # Yahoo stamps each daily bar at the session's OPEN in UTC; converted to IST
    # that lands on the correct calendar date, which is what the screeners key on
    # (they compare `df.index.date`). Without the conversion an 03:45 UTC bar is
    # still the right date, but a late-session-open exchange would not be.
    df.index = df.index.tz_convert("Asia/Kolkata").tz_localize(None).normalize()
    df.index.name = "datetime"
    # A holiday or halted day can come back as an all-null row; leaving it in
    # would let a NaN Low silently poison the 52-week-low comparison.
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df[["Open", "High", "Low", "Close", "Volume"]]


def fetch_daily_many(symbols: list[str], days: int = 850, progress_every: int = 25) -> dict[str, pd.DataFrame]:
    """Daily bars for a whole universe, skipping symbols that fail."""
    data: dict[str, pd.DataFrame] = {}
    for i, symbol in enumerate(symbols, start=1):
        df = fetch_daily(symbol, days=days)
        if not df.empty:
            data[symbol] = df
        if progress_every and i % progress_every == 0:
            print(f"  ... {i}/{len(symbols)} symbols fetched ({len(data)} with data)")
        time.sleep(REQUEST_DELAY_SECONDS)
    return data
