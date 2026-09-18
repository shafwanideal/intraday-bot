"""Scan a universe for stocks whose most recent (or currently forming) weekly
candle bounced off a rolling weekly support level -- see
`src/weekly_support.py` for exactly what "support" and "bounce" mean here.

This is a one-shot scan, not a backtest: it reports what's true as of the
latest available daily bar, for you to go look at the chart.

Usage: python3 scripts/run_weekly_support_scan.py [universe] [source] [lookback_weeks]
  universe: nifty50 | nifty200 (default) | nifty500
  source: yahoo (default, free & unauthenticated) | kite (needs a same-day
    `python3 -m src.auth` login, or KITE_ACCESS_TOKEN set in the environment)
  lookback_weeks: weeks of prior history the support floor is drawn from (default 10)
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import kite_data, screener, weekly_support, yahoo_daily

DEFAULT_UNIVERSE = "nifty200"
DEFAULT_SOURCE = "yahoo"
DAILY_HISTORY_DAYS = 400  # comfortably covers lookback_weeks=10 + the tested week + buffer

UNIVERSE_LOADERS = {
    "nifty50": screener.load_nifty50_symbols,
    "nifty200": screener.load_nifty200_symbols,
    "nifty500": screener.load_nifty500_symbols,
}

# (fetch_daily(symbol, days=...) -> DataFrame, inter-request delay seconds)
DATA_SOURCES = {
    "yahoo": (yahoo_daily.fetch_daily, yahoo_daily.REQUEST_DELAY_SECONDS),
    "kite": (kite_data.fetch_daily, kite_data.REQUEST_DELAY_SECONDS),
}


def main() -> None:
    universe = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_UNIVERSE
    source = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_SOURCE
    lookback_weeks = int(sys.argv[3]) if len(sys.argv) > 3 else weekly_support.DEFAULT_LOOKBACK_WEEKS
    fetch_daily, request_delay = DATA_SOURCES[source]

    symbols = UNIVERSE_LOADERS[universe]()
    print(f"Universe: {len(symbols)} {universe} symbols, via {source}")

    print(f"Fetching {DAILY_HISTORY_DAYS}d of daily bars for all {len(symbols)} symbols ...")
    daily_data: dict = {}
    for i, sym in enumerate(symbols):
        df = fetch_daily(sym, days=DAILY_HISTORY_DAYS)
        if not df.empty:
            daily_data[sym] = df
        if (i + 1) % 25 == 0:
            print(f"  ... {i + 1}/{len(symbols)} fetched ({len(daily_data)} with data)")
        time.sleep(request_delay)
    print(f"Got data for {len(daily_data)}/{len(symbols)} symbols\n")

    results = weekly_support.screen_weekly_support_bounces(daily_data, lookback_weeks=lookback_weeks)

    print(f"{'=' * 78}")
    print(f"WEEKLY SUPPORT BOUNCES ({universe} via {source}, {lookback_weeks}-week support floor)")
    print(f"{'=' * 78}")
    if not results:
        print("No bounces found.")
        return

    for r in results:
        tag = "THIS WEEK (in progress)" if r["in_progress"] else "last completed week"
        print(
            f"{r['symbol']:12s}  {tag:24s}  week ending {r['week_ending']}  "
            f"support={r['support']:.2f}  low={r['low']:.2f}  close={r['close']:.2f}  "
            f"({r['pct_above_support'] * 100:+.1f}% above support, "
            f"closed in top {r['recovery_pct'] * 100:.0f}% of week's range)"
        )

    print(
        "\nCAVEATS:\n"
        "- Support is a rolling weekly swing-low, not a pivot-point formula or anyone's manually\n"
        "  drawn trendline -- a real floor, but not the only valid way to define one.\n"
        "- 'Bounce' is a mechanical pattern match on OHLC alone (touch + reclaim + strong close),\n"
        "  not a judgment on the stock's fundamentals or the reason for the move.\n"
        "- The in-progress week's bar is incomplete -- it can still close back below support by Friday."
    )


if __name__ == "__main__":
    main()
