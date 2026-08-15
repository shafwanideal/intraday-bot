import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import backtest, data

SYMBOLS = ["ICICIBANK", "HFCL", "CUPID", "TCS", "WIPRO", "INFY"]
DIRECTIONS: dict[str, str] = {}  # e.g. {"HFCL": "short"} to flag a short for the day
GRID_PCTS_TO_COMPARE = [0.01, 0.02]

CAVEATS = """
CAVEATS (read before acting on these numbers):
- Data: Yahoo Finance free intraday data, 5-minute bars, last ~60 calendar
  days only (~58 trading days). This is real market data, not synthetic,
  but it is a short/recent window, not a multi-year backtest.
- Grid trigger levels are evaluated on each 5-min bar's CLOSE, not tick by
  tick, so intrabar breaches may be caught late or missed.
- Only ONE averaging leg is modeled: entry + one add-on, not a multi-level
  grid.
- Quantities are fractional (capital / price), not rounded to real share
  lots.
- Transaction costs ARE now modeled (Zerodha intraday equity: brokerage,
  STT, exchange charges, SEBI charges, stamp duty, GST) and deducted per
  order. Slippage is still NOT modeled.
- Entries assume every symbol enters LONG at the market open unless flagged
  SHORT in DIRECTIONS above.
- The real stock list is only known at 9 AM each morning; SYMBOLS here is a
  fixed sample set for backtesting, not the actual daily list.
"""


def main() -> None:
    print(f"Fetching intraday data for {SYMBOLS} ...")
    bars = data.fetch_many(SYMBOLS, period="60d", interval="5m")

    if not bars:
        print("No data fetched for any symbol. Aborting.")
        sys.exit(1)

    for grid_pct in GRID_PCTS_TO_COMPARE:
        print(f"\n{'=' * 70}\nGRID = {grid_pct:.0%}  (both averaging step and exit target)\n{'=' * 70}")

        print("\n--- Portfolio backtest (all symbols competing for 3 slots / 4 units) ---")
        results = backtest.run_backtest(bars, symbols=SYMBOLS, directions=DIRECTIONS, grid_pct=grid_pct)
        print(backtest.summarize(results))

        print("\n--- Per-symbol isolated comparison (each gets full capital) ---")
        comparison = backtest.per_symbol_comparison(bars, symbols=SYMBOLS, directions=DIRECTIONS, grid_pct=grid_pct)
        print(comparison.to_string(index=False))

    print(CAVEATS)


if __name__ == "__main__":
    main()
