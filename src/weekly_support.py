"""Weekly support-bounce screener.

"Weekly support" here means a rolling swing-low: the lowest weekly Low over
the `lookback_weeks` weeks strictly before the week being tested. That's a
level other traders on the weekly chart are actually watching (a real prior
floor), not a single day's pivot-point arithmetic.

A "bounce" is a week that:
  1. Touched down to within `touch_tolerance` of that floor (or broke slightly
     below it intraweek) -- i.e. it actually tested support, not just traded
     somewhere above it.
  2. Closed back ABOVE the support level -- it reclaimed the floor rather than
     breaking down through it.
  3. Closed in at least the upper `min_recovery` fraction of that week's own
     High-Low range -- filters out a week that merely touched support and
     closed near its own low (still weak / possibly still breaking down).

Source-agnostic: works on any (symbol -> daily OHLC DataFrame) dict, whether
the bars came from `src.kite_data.fetch_daily` or `src.yahoo_daily.fetch_daily`
-- same DataFrame shape (Open/High/Low/Close/Volume, DatetimeIndex) either way.
"""

import pandas as pd

DEFAULT_LOOKBACK_WEEKS = 10
DEFAULT_TOUCH_TOLERANCE = 0.02  # within 2% of (or below) the support level counts as a "touch"
DEFAULT_MIN_RECOVERY = 0.5  # close must land in at least the upper half of that week's range


def weekly_ohlc(daily_df: pd.DataFrame) -> pd.DataFrame:
    """Resample daily OHLCV bars into NSE-week (Mon-Fri, bucketed to the
    Friday) candles. Drops any week with no trading data (holiday-only weeks
    at the edges of the requested range)."""
    if daily_df.empty:
        return daily_df
    weekly = daily_df.resample("W-FRI").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    )
    return weekly.dropna(subset=["Open", "High", "Low", "Close"])


def weekly_support_level(weekly_df: pd.DataFrame, idx: int, lookback_weeks: int = DEFAULT_LOOKBACK_WEEKS) -> float | None:
    """Support for the week at position `idx`: the lowest weekly Low over the
    `lookback_weeks` weeks strictly before it (never including the week being
    tested itself -- otherwise a deep sell-off week would define its own
    floor). None if there isn't enough prior history yet."""
    if idx < lookback_weeks:
        return None
    prior = weekly_df.iloc[idx - lookback_weeks : idx]
    return float(prior["Low"].min())


def check_bounce(
    weekly_df: pd.DataFrame,
    idx: int,
    lookback_weeks: int = DEFAULT_LOOKBACK_WEEKS,
    touch_tolerance: float = DEFAULT_TOUCH_TOLERANCE,
    min_recovery: float = DEFAULT_MIN_RECOVERY,
) -> dict | None:
    """None if the week at `idx` doesn't qualify as a support bounce (or
    there isn't enough history), else a dict of the levels involved."""
    support = weekly_support_level(weekly_df, idx, lookback_weeks)
    if support is None:
        return None
    week = weekly_df.iloc[idx]
    low, high, close = float(week["Low"]), float(week["High"]), float(week["Close"])
    if high <= low:
        return None  # degenerate single-tick week, nothing to measure a range against

    touched_support = low <= support * (1 + touch_tolerance)
    reclaimed_support = close > support
    recovery_pct = (close - low) / (high - low)
    if not (touched_support and reclaimed_support and recovery_pct >= min_recovery):
        return None

    return {
        "week_ending": weekly_df.index[idx].date(),
        "support": support,
        "low": low,
        "close": close,
        "pct_above_support": (close - support) / support,
        "recovery_pct": recovery_pct,
    }


def screen_weekly_support_bounces(
    daily_data: dict[str, pd.DataFrame],
    lookback_weeks: int = DEFAULT_LOOKBACK_WEEKS,
    touch_tolerance: float = DEFAULT_TOUCH_TOLERANCE,
    min_recovery: float = DEFAULT_MIN_RECOVERY,
) -> list[dict]:
    """Every symbol whose most recent weekly candle -- completed or still
    forming -- is a support bounce by `check_bounce`'s definition. Checks the
    current (possibly in-progress) week first so a bounce already underway is
    reported before Friday's close finalizes it; falls back to the last
    completed week if the current one doesn't qualify. At most one result per
    symbol. Sorted by recovery strength (how far up its own range the week
    closed), strongest first.
    """
    results = []
    for symbol, daily_df in daily_data.items():
        weekly = weekly_ohlc(daily_df)
        if len(weekly) < lookback_weeks + 1:
            continue
        last_idx = len(weekly) - 1
        for idx, in_progress in ((last_idx, True), (last_idx - 1, False)):
            if idx < lookback_weeks:
                continue
            bounce = check_bounce(weekly, idx, lookback_weeks, touch_tolerance, min_recovery)
            if bounce:
                bounce["symbol"] = symbol
                bounce["in_progress"] = in_progress
                results.append(bounce)
                break

    results.sort(key=lambda r: r["recovery_pct"], reverse=True)
    return results
