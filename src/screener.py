"""Automated version of the Groww "Bullish Stocks Near Breakout" screening
setup (see the PDF the user shared, 2026-09-02): Nifty 200 universe (stands
in for "Nifty 500 filtered to Large + Mid Cap" -- Kite has no market-cap
field, and Nifty 200 = Nifty 100 + Nifty Midcap 100, which IS exactly that
universe, just via a different exact list than Groww's own), then a manual
checklist (EMA stack, volume trend, 52-week-high proximity, R1 pivot
proximity, MACD) applied on top, sorted by volume.

Entirely long-only, matching the PDF's own framing ("bullish-stock
averaging strategy").
"""

import csv
from datetime import date
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
NIFTY200_CSV = DATA_DIR / "nifty200.csv"

# How close to the 52-week high counts as "reasonably close" -- within 10%.
NEAR_52W_HIGH_PCT = 0.10
# "Near R1" band: close is somewhere between the pivot and just past R1 --
# catches both "approaching" and "just broken out over" R1, not something
# already extended far beyond it or nowhere near it.
R1_BAND_LOW = 0.97   # 3% below R1
R1_BAND_HIGH = 1.05  # 5% above R1
EMA_RISING_LOOKBACK = 5  # trading days back to compare EMA20 against, for "rising"
MIN_HISTORY_DAYS = 260  # need >= this many daily bars before the as-of date (200 EMA + buffer)


def load_nifty200_symbols() -> list[str]:
    """Nifty 200 constituents, from a point-in-time NSE snapshot (data/nifty200.csv,
    fetched from archives.nseindia.com). Index membership does drift over time --
    treat this as a snapshot, not a live feed."""
    with open(NIFTY200_CSV) as f:
        return [row["Symbol"] for row in csv.DictReader(f)]


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def compute_screen_metrics(daily_df: pd.DataFrame, as_of: date) -> dict | None:
    """All metrics computed using only bars strictly BEFORE `as_of` -- this is
    what a screen run before market open on `as_of` would actually have seen,
    avoiding lookahead into the day being traded. Returns None if there isn't
    enough history yet.
    """
    hist = daily_df[daily_df.index.date < as_of]
    if len(hist) < MIN_HISTORY_DAYS:
        return None

    close = hist["Close"]
    ema20 = _ema(close, 20)
    ema50 = _ema(close, 50)
    ema200 = _ema(close, 200)

    macd_line = _ema(close, 12) - _ema(close, 26)
    macd_signal = _ema(macd_line, 9)

    high_252 = hist["High"].tail(252).max()

    prev = hist.iloc[-1]
    pivot = (prev["High"] + prev["Low"] + prev["Close"]) / 3
    r1 = 2 * pivot - prev["Low"]

    avg_volume_20 = hist["Volume"].tail(20).mean()

    return {
        "close": float(close.iloc[-1]),
        "ema20": float(ema20.iloc[-1]),
        "ema20_prev": float(ema20.iloc[-1 - EMA_RISING_LOOKBACK]),
        "ema50": float(ema50.iloc[-1]),
        "ema200": float(ema200.iloc[-1]),
        "macd_line": float(macd_line.iloc[-1]),
        "macd_signal": float(macd_signal.iloc[-1]),
        "high_52w": float(high_252),
        "r1": float(r1),
        "volume": float(prev["Volume"]),
        "avg_volume_20": float(avg_volume_20),
    }


def passes_checklist(m: dict) -> bool:
    """The PDF's manual checklist, as boolean conditions:
    - Price > 20 EMA > 50 EMA > 200 EMA
    - EMAs (20) preferably rising
    - Volume improving (above its own 20-day average)
    - Reasonably close to 52-week high
    - MACD supportive (line above signal)
    - Near R1 pivot (approaching or just past it, not extended)
    """
    return (
        m["close"] > m["ema20"] > m["ema50"] > m["ema200"]
        and m["ema20"] > m["ema20_prev"]
        and m["volume"] > m["avg_volume_20"]
        and m["close"] >= m["high_52w"] * (1 - NEAR_52W_HIGH_PCT)
        and m["macd_line"] > m["macd_signal"]
        and R1_BAND_LOW * m["r1"] <= m["close"] <= R1_BAND_HIGH * m["r1"]
    )


def screen_day(daily_data: dict[str, pd.DataFrame], as_of: date, top_n: int = 3) -> list[str]:
    """Symbols passing the full checklist as of `as_of` (using only prior
    data), sorted by yesterday's volume descending, capped at `top_n`."""
    candidates = []
    for symbol, df in daily_data.items():
        m = compute_screen_metrics(df, as_of)
        if m is not None and passes_checklist(m):
            candidates.append((symbol, m["volume"]))
    candidates.sort(key=lambda x: x[1], reverse=True)
    return [sym for sym, _ in candidates[:top_n]]
