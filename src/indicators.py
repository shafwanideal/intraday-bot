import pandas as pd


def atr(daily_df: pd.DataFrame, period: int = 14) -> float | None:
    """Average True Range from daily OHLC bars (needs High/Low/Close columns).

    Returns the latest ATR value (absolute price units), or None if there
    isn't enough history to compute a full `period`-length average.
    """
    if len(daily_df) <= period:
        return None
    high, low, close = daily_df["High"], daily_df["Low"], daily_df["Close"]
    prev_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    value = true_range.rolling(period).mean().iloc[-1]
    return float(value) if pd.notna(value) else None
