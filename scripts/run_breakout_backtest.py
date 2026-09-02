"""Backtest the Groww breakout-screening setup (data/nifty200.csv + the
manual checklist in src/screener.py) feeding into the grid strategy, using
whatever settings are CURRENTLY live in src/strategy.py -- not a separately
maintained copy of them, so this always reflects the real config.

For each of the last N trading days: screen using only data available
before that day's open (no lookahead), take the top TOP_N by volume among
symbols passing the checklist, trade them all long (this is a bullish-only
screener) with the live grid engine settings, using real intraday bars.
"""

import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import backtest, kite_data, screener, strategy

LOOKBACK_TRADING_DAYS = 60
TOP_N = 3
DAILY_HISTORY_DAYS = 500  # calendar days of daily bars per symbol -- enough for 200 EMA + 52w high + the backtest window


def main() -> None:
    symbols = screener.load_nifty200_symbols()
    print(f"Universe: {len(symbols)} Nifty 200 symbols")

    print(f"Fetching {DAILY_HISTORY_DAYS}d of daily bars for all {len(symbols)} symbols (screening data) ...")
    daily_data: dict = {}
    for i, sym in enumerate(symbols):
        df = kite_data.fetch_daily(sym, days=DAILY_HISTORY_DAYS)
        if not df.empty:
            daily_data[sym] = df
        if (i + 1) % 25 == 0:
            print(f"  ... {i + 1}/{len(symbols)}")
        time.sleep(kite_data.REQUEST_DELAY_SECONDS)

    # Trading days to screen+trade: the most recent LOOKBACK_TRADING_DAYS calendar
    # dates that actually have bars, from the most liquid symbol's own index.
    all_dates = sorted({d for df in daily_data.values() for d in df.index.date})
    test_days = all_dates[-LOOKBACK_TRADING_DAYS:]
    print(f"Backtest window: {test_days[0]} to {test_days[-1]} ({len(test_days)} trading days)")

    print("Screening each day (no lookahead -- only prior data used per day) ...")
    daily_plan: dict = {}
    for d in test_days:
        picks = screener.screen_day(daily_data, d, top_n=TOP_N)
        if picks:
            daily_plan[d] = {sym: "long" for sym in picks}

    days_with_picks = len(daily_plan)
    print(f"{days_with_picks}/{len(test_days)} days had at least one qualifying stock")
    if not daily_plan:
        print("No days had any qualifying picks -- nothing to backtest.")
        return

    picked_symbols = sorted({sym for day_picks in daily_plan.values() for sym in day_picks})
    print(f"Unique symbols picked across the window: {picked_symbols}")

    print("Fetching 5-min intraday bars for the picked symbols ...")
    intraday_bars = kite_data.fetch_many(picked_symbols, days=LOOKBACK_TRADING_DAYS + 15, interval="5minute")

    print("Fetching ATR (14d) for the picked symbols ...")
    symbol_atr = kite_data.fetch_symbol_atr(picked_symbols)

    # Mirror live.py's own sizing ratio for a TOP_N-stock day: TOP_N concurrent slots,
    # one spare unit shared for averaging (matches live.py's max(len(plan),1)+1 pattern).
    max_concurrent_positions = TOP_N
    total_units = TOP_N + 1

    results = backtest.run_backtest(
        intraday_bars,
        daily_plan=daily_plan,
        grid_pct=strategy.GRID_PCT,
        averaging_pct=strategy.AVERAGING_PCT,
        enable_averaging=strategy.ENABLE_AVERAGING,
        trail_stop=strategy.TRAIL_STOP,
        profit_exit=strategy.PROFIT_EXIT,
        atr_multiplier=strategy.DEFAULT_ATR_MULTIPLIER,
        symbol_atr=symbol_atr,
        per_stock_stop_loss=strategy.PER_STOCK_STOP_LOSS,
        portfolio_profit_lock_trigger=strategy.PORTFOLIO_PROFIT_LOCK_TRIGGER,
        portfolio_profit_lock_giveback=strategy.PORTFOLIO_PROFIT_LOCK_GIVEBACK,
        daily_loss_cap=strategy.compute_daily_loss_cap(strategy.MARGIN_CAPITAL),
        margin_capital=strategy.MARGIN_CAPITAL,
        total_units=total_units,
        max_concurrent_positions=max_concurrent_positions,
    )

    print(f"\n{'=' * 70}")
    print("Breakout-screener backtest (using CURRENT live strategy settings)")
    print(f"grid_pct={strategy.GRID_PCT:.1%}  averaging_pct={strategy.AVERAGING_PCT:.1%} (enabled={strategy.ENABLE_AVERAGING})")
    print(f"trail_stop={strategy.TRAIL_STOP}  profit_exit={strategy.PROFIT_EXIT}  atr_mult={strategy.DEFAULT_ATR_MULTIPLIER}")
    print(f"per_stock_stop_loss=Rs {strategy.PER_STOCK_STOP_LOSS}  portfolio_profit_lock={strategy.PORTFOLIO_PROFIT_LOCK_TRIGGER}/{strategy.PORTFOLIO_PROFIT_LOCK_GIVEBACK}")
    print(f"{'=' * 70}\n")
    print(backtest.summarize(results, goal_daily_pnl=2000))

    print("\n--- Day by day ---")
    import pandas as pd

    daily = pd.DataFrame(results["daily_results"]).sort_values("date")
    trades = pd.DataFrame(results["trade_log"])
    for _, row in daily.iterrows():
        day = row["date"]
        picks = ", ".join(daily_plan.get(day, {}).keys())
        note = f"  [{row['note']}]" if isinstance(row.get("note"), str) else ""
        print(f"{day}  {picks:30s} net P&L: Rs {row['pnl']:>10,.2f}{note}")
        if not trades.empty:
            day_trades = trades[trades["date"] == day]
            for _, tr in day_trades.iterrows():
                print(
                    f"    {tr['symbol']:12s} avg={tr['avg_price']:.2f} exit={tr['exit_price']:.2f} "
                    f"qty={tr['qty']:.1f} net={tr['pnl']:>9,.2f} ({tr['reason']})"
                )

    print(
        "\nCAVEATS:\n"
        "- Universe is Nifty 200 (large+mid cap proxy), not Groww's exact Nifty 500-filtered-to-cap list.\n"
        "- 'Near R1' and '52w-high proximity' bands are this build's own numeric interpretation of the\n"
        "  PDF's qualitative checklist, not Groww's own scanner logic -- Groww's internal breakout/pivot\n"
        "  scanner isn't replicated exactly since it's proprietary.\n"
        "- All picks are long-only, matching the PDF's own bullish-only framing.\n"
        "- Settings shown above are pulled live from src/strategy.py at run time."
    )


if __name__ == "__main__":
    main()
