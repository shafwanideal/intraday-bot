import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import backtest, data

SYMBOLS = ["ICICIBANK", "HFCL", "CUPID", "TCS", "WIPRO", "INFY"]
DIRECTIONS: dict[str, str] = {}  # e.g. {"HFCL": "short"} to flag a short for the day

CAVEATS = """
CAVEATS (read before acting on these numbers):
- Data: Yahoo Finance free intraday data, 5-minute bars, last ~60 calendar
  days only (~58 trading days). This is real market data, not synthetic,
  but it is a short/recent window, not a multi-year backtest.
- 2% trigger levels are evaluated on each 5-min bar's CLOSE, not tick by
  tick, so intrabar breaches may be caught late or missed.
- Only ONE averaging leg is modeled (per the brief's literal wording):
  entry + one add-on, not a multi-level grid.
- Quantities are fractional (capital / price), not rounded to real share
  lots.
- No brokerage, STT, exchange charges, GST, stamp duty, or slippage are
  deducted. Real net P&L will be lower than shown here.
- Entries assume every symbol enters LONG at the market open unless flagged
  SHORT in DIRECTIONS above.
"""


def main() -> None:
    print(f"Fetching intraday data for {SYMBOLS} ...")
    bars = data.fetch_many(SYMBOLS, period="60d", interval="5m")

    if not bars:
        print("No data fetched for any symbol. Aborting.")
        sys.exit(1)

    print("\n=== Portfolio backtest (all symbols competing for 3 slots / 4 units, entries in list order) ===")
    results = backtest.run_backtest(bars, symbols=SYMBOLS, directions=DIRECTIONS)
    print(backtest.summarize(results))

    print("\n=== Per-symbol isolated comparison (each gets full capital, no slot competition) ===")
    comparison = backtest.per_symbol_comparison(bars, symbols=SYMBOLS, directions=DIRECTIONS)
    print(comparison.to_string(index=False))

    print(CAVEATS)


if __name__ == "__main__":
    main()
