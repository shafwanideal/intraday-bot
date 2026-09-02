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


def rsi(daily_df: pd.DataFrame, period: int = 14) -> float | None:
    """Wilder's RSI from daily OHLC bars (needs a Close column). Returns the
    latest value (0-100), or None if there isn't enough history for a full
    `period`-length smoothed average."""
    if len(daily_df) <= period:
        return None
    delta = daily_df["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    # Wilder smoothing = an EMA with alpha = 1/period (not a plain rolling mean).
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    last_gain, last_loss = avg_gain.iloc[-1], avg_loss.iloc[-1]
    if pd.isna(last_gain) or pd.isna(last_loss):
        return None
    if last_loss == 0:
        return 100.0
    rs = last_gain / last_loss
    return float(100 - (100 / (1 + rs)))
