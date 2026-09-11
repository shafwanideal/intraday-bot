import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import backtest, kite_data

# Today's picks, entered as a single dated plan (like run_daily_plan_backtest.py)
# rather than a repeating rule -- this only simulates the ONE trading day below.
# Each symbol carries an explicit pct of margin_capital (2026-09-11 rule change:
# capital is allocated by percentage per stock, not split evenly across slots --
# see backtest.validate_day_plan_allocations, which requires every symbol in a
# day to give one, or none at all). "INDIGO" -> InterGlobe Aviation, "GRANULES"
# -> Granules India, "HDFCBANK" -> HDFC Bank (there is no standalone "HDFC"
# ticker since the 2023 merger into HDFC Bank). Verify tradingsymbols against
# Kite's instrument list if unsure -- a typo'd symbol is silently skipped.
TODAY = date.today()
DAILY_PLAN: dict[date, dict[str, str | dict]] = {
    TODAY: {
        "INDIGO": {"direction": "short", "pct": 33},
        "GRANULES": {"direction": "long", "pct": 33},
        "HDFCBANK": {"direction": "long", "pct": 33},
    },
}
GRID_PCT = 0.015
GOAL_DAILY_PNL = 2_000

CAVEATS = """
CAVEATS (read before acting on these numbers):
- Data: Kite Connect historical data API, 5-minute bars. Requires a same-day
  Kite login (python3 -m src.auth).
- This simulates ONLY the single date above (today). If today's session
  hasn't happened yet or Kite has no data for it (e.g. run before market
  close, or a non-trading day), this will report "no data for this day's
  symbol(s)" -- run it again after market close.
- Each symbol enters at today's real market open (9:15 AM) at its stated
  pct of margin_capital (leveraged) -- not an equal split. Percentages must
  either all be given (this plan) or all omitted; see
  backtest.validate_day_plan_allocations.
- No averaging leg -- enable_averaging=False, one entry per symbol, no
  add-on ever, matching the 2026-09-11 rule change.
- A trailing stop arms once a position moves GRID_PCT (1.5%) in its favor,
  then trails from the best price seen -- see run_backtest's trail_stop/
  atr_multiplier docs for exactly how the trail distance is computed.
- Grid trigger levels are evaluated on each 5-min bar's CLOSE, not tick by
  tick.
- Leverage modeled at a flat 5x on margin -- verify actual Zerodha MIS
  leverage per symbol via Kite's margin calculator before trading live.
- Transaction costs ARE modeled (brokerage, STT, exchange charges, SEBI
  charges, stamp duty, GST) on the leveraged order value. Slippage is NOT
  modeled.
- A single day is not a sample size -- one good or bad outcome here says
  very little about whether this was a good pick, only what happened.
"""


def main() -> None:
    symbols = sorted({sym for day_plan in DAILY_PLAN.values() for sym in day_plan})
    plan_today = DAILY_PLAN.get(TODAY, {})
    print("Today's plan:")
    for sym, entry in plan_today.items():
        direction, pct = backtest.parse_plan_entry(entry)
        alloc = f"{pct:.0f}% of margin capital" if pct is not None else "equal split"
        print(f"  {sym:10s} {direction:5s} {alloc}")
    print(f"\nFetching intraday data for {symbols} from Kite (this re-authenticates if needed) ...")
    bars = kite_data.fetch_many(symbols, days=5, interval="5minute")

    if not bars:
        print("No data fetched for any symbol. Aborting.")
        sys.exit(1)

    print("Fetching ATR (14d) for volatility-scaled trailing stop ...")
    symbol_atr = kite_data.fetch_symbol_atr(symbols)

    results = backtest.run_backtest(
        bars,
        daily_plan=DAILY_PLAN,
        grid_pct=GRID_PCT,
        symbol_atr=symbol_atr,
        atr_multiplier=backtest.DEFAULT_ATR_MULTIPLIER,
        # 2026-09-11 rule change: no averaging leg at all. run_backtest's own
        # default is enable_averaging=True (a research knob, separate from
        # live.py/shadow.py's ENABLE_AVERAGING constant) -- must be set
        # explicitly here or this script would silently still average.
        enable_averaging=False,
        # trail_stop/profit_exit are left at run_backtest's own defaults
        # (both True) -- that already matches today's rule: arm a trailing
        # stop once a position moves grid_pct (1.5%) in its favor.
    )

    print(f"\n{'=' * 70}\nToday's-picks backtest: {TODAY}, grid={GRID_PCT:.0%}, leverage={backtest.LEVERAGE}x\n{'=' * 70}\n")
    print(backtest.summarize(results, goal_daily_pnl=GOAL_DAILY_PNL))

    print("\n--- Trade log ---")
    import pandas as pd

    trades = pd.DataFrame(results["trade_log"])
    if trades.empty:
        print("(no trades -- see daily_results note above for why)")
    else:
        for _, tr in trades.iterrows():
            print(
                f"    {tr['symbol']:12s} {tr['direction']:5s} avg={tr['avg_price']:.2f} "
                f"exit={tr['exit_price']:.2f} qty={tr['qty']:.1f} units={tr['units_used']} "
                f"gross={tr['gross_pnl']:>9,.2f} costs={tr['costs']:>6,.2f} net={tr['pnl']:>9,.2f} ({tr['reason']})"
            )

    print(CAVEATS)


if __name__ == "__main__":
    main()
