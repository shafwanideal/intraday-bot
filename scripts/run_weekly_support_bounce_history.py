"""How many times has each stock in a universe bounced off its own rolling
weekly support -- see `src/weekly_support.find_bounce_events` for exactly
what counts as one bounce event (touch + reclaim + strong close, with a
multi-week consolidation sitting right at support collapsed into a single
event rather than counted once per week).

More days of history feeding the same lookback_weeks naturally surfaces more
events, so counts are only comparable across runs using the same `days` and
`lookback_weeks` settings -- this is a "how proven is this floor" read, not
an apples-to-apples score.

Usage: python3 scripts/run_weekly_support_bounce_history.py [universe] [source] [lookback_weeks] [days]
  universe: nifty50 | nifty200 (default) | nifty500
  source: yahoo (default, free & unauthenticated) | kite (needs a same-day
    `python3 -m src.auth` login, or KITE_ACCESS_TOKEN set in the environment)
  lookback_weeks: weeks of prior history each support floor is drawn from (default 10)
  days: daily bars of history to pull per symbol (default 900, ~2.5 years)
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import kite_data, screener, weekly_support, yahoo_daily

DEFAULT_UNIVERSE = "nifty200"
DEFAULT_SOURCE = "yahoo"
DEFAULT_DAYS = 900

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
    days = int(sys.argv[4]) if len(sys.argv) > 4 else DEFAULT_DAYS
    fetch_daily, request_delay = DATA_SOURCES[source]

    symbols = UNIVERSE_LOADERS[universe]()
    print(f"Universe: {len(symbols)} {universe} symbols, via {source}, {days}d history")

    print(f"Fetching {days}d of daily bars for all {len(symbols)} symbols ...")
    daily_data: dict = {}
    for i, sym in enumerate(symbols):
        df = fetch_daily(sym, days=days)
        if not df.empty:
            daily_data[sym] = df
        if (i + 1) % 25 == 0:
            print(f"  ... {i + 1}/{len(symbols)} fetched ({len(daily_data)} with data)")
        time.sleep(request_delay)
    print(f"Got data for {len(daily_data)}/{len(symbols)} symbols\n")

    results = weekly_support.screen_bounce_history(daily_data, lookback_weeks=lookback_weeks)

    print(f"{'=' * 78}")
    print(f"WEEKLY SUPPORT BOUNCE HISTORY ({universe} via {source}, {lookback_weeks}-week floor, {days}d lookback)")
    print(f"{'=' * 78}")
    if not results:
        print("No bounce history found.")
        return

    for r in results:
        dates = ", ".join(str(e["week_ending"]) for e in r["events"])
        print(f"{r['symbol']:12s}  {r['count']:>2d} bounce(s) over {r['weeks_of_data']} weeks of data  -- {dates}")

    print(
        "\nCAVEATS:\n"
        "- Counts bounces off THIS rolling 10-week-swing-low definition specifically, not every\n"
        "  possible support/resistance a chartist might draw -- a different lookback_weeks counts differently.\n"
        "- More days of history naturally surfaces more events; only compare counts run with the same\n"
        "  `days` and `lookback_weeks` settings.\n"
        "- A high count says the level has repeatedly attracted buyers before -- not that it will again."
    )


if __name__ == "__main__":
    main()
