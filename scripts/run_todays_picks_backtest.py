import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import backtest, kite_data

# Today's picks, entered as a single dated plan (like run_daily_plan_backtest.py)
# rather than a repeating rule -- this only simulates the ONE trading day below.
# "rvn" -> RVNL (Rail Vikas Nigam Ltd), "mazadoc shipbuilding" -> MAZDOCK
# (Mazagon Dock Shipbuilders Ltd). Verify both tradingsymbols against Kite's
# instrument list if unsure -- a typo'd symbol is silently skipped.
TODAY = date.today()
DAILY_PLAN: dict[date, dict[str, str]] = {
    TODAY: {"RVNL": "long", "MAZDOCK": "long"},
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
- Both symbols enter LONG at today's real market open (9:15 AM), one unit
  each -- they share the same 3-slot / 4-unit pool, so both can hold a
  position simultaneously here since there's only 2 of them.
- Grid trigger levels are evaluated on each 5-min bar's CLOSE, not tick by
  tick.
- Only ONE averaging leg is modeled: entry + one add-on, not a multi-level
  grid.
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
    print(f"Fetching intraday data for {symbols} from Kite (this re-authenticates if needed) ...")
    bars = kite_data.fetch_many(symbols, days=5, interval="5minute")

    if not bars:
        print("No data fetched for any symbol. Aborting.")
        sys.exit(1)

    print("Fetching ATR (14d) for volatility-scaled trailing stop ...")
    symbol_atr = kite_data.fetch_symbol_atr(symbols)

    results = backtest.run_backtest(
        bars, daily_plan=DAILY_PLAN, grid_pct=GRID_PCT, symbol_atr=symbol_atr, atr_multiplier=backtest.DEFAULT_ATR_MULTIPLIER
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
