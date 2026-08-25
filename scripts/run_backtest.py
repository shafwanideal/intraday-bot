import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import backtest, kite_data

SYMBOLS = ["ICICIBANK", "HFCL", "CUPID", "TCS", "WIPRO", "INFY"]
STRONG_CANDIDATES = ["HFCL", "CUPID", "ICICIBANK"]  # best performers so far -- selection-biased, see caveats
DIRECTIONS: dict[str, str] = {}  # e.g. {"HFCL": "short"} to flag a short for the day
GRID_PCT = 0.015  # revised from 2% on 2026-08-25
GOAL_DAILY_PNL = 2_000

CAVEATS = """
CAVEATS (read before acting on these numbers):
- Data: Kite Connect historical data API, 5-minute bars, last ~180 calendar
  days. Same feed Kite quotes/shadow mode use, so no cross-source mismatch
  with live trading -- but still not a multi-year backtest.
- Grid trigger levels are evaluated on each 5-min bar's CLOSE, not tick by
  tick, so intrabar breaches may be caught late or missed.
- Only ONE averaging leg is modeled: entry + one add-on, not a multi-level
  grid.
- Leverage is modeled at a flat 5x on margin (as stated by the user), but
  real Zerodha MIS leverage varies per stock -- verify per-symbol via Kite's
  margin calculator before trading live.
- Quantities are fractional (capital / price), not rounded to real share
  lots.
- Transaction costs ARE modeled (Zerodha intraday equity: brokerage, STT,
  exchange charges, SEBI charges, stamp duty, GST) and deducted per order,
  now correctly computed on the LEVERAGED order value. Slippage is still
  NOT modeled.
- Entries assume every symbol enters LONG at the market open unless flagged
  SHORT in DIRECTIONS above.
- The real stock list is only known at 9 AM each morning; SYMBOLS here is a
  fixed sample set for backtesting, not the actual daily list. The
  STRONG_CANDIDATES subset is chosen with hindsight from this same 58-day
  window -- treat its numbers as optimistic, not a guarantee future lists
  will look like this.
"""


def main() -> None:
    print(f"Fetching intraday data for {SYMBOLS} from Kite (this re-authenticates if needed) ...")
    bars = kite_data.fetch_many(SYMBOLS, days=180, interval="5minute")

    if not bars:
        print("No data fetched for any symbol. Aborting.")
        sys.exit(1)

    print("Fetching ATR (14d) for volatility-scaled trailing stop ...")
    symbol_atr = kite_data.fetch_symbol_atr(SYMBOLS)

    print(f"\n{'=' * 70}\nGRID = {GRID_PCT:.0%}, LEVERAGE = {backtest.LEVERAGE}x on Rs {backtest.MARGIN_CAPITAL:,} margin, ATR trail\n{'=' * 70}")

    print(f"\n--- Full 6-symbol portfolio (3 slots / 4 units, entries in list order) ---")
    full_results = backtest.run_backtest(
        bars, symbols=SYMBOLS, directions=DIRECTIONS, grid_pct=GRID_PCT, symbol_atr=symbol_atr, atr_multiplier=backtest.DEFAULT_ATR_MULTIPLIER
    )
    print(backtest.summarize(full_results, goal_daily_pnl=GOAL_DAILY_PNL))

    print(f"\n--- Strong-candidate-only portfolio {STRONG_CANDIDATES} (selection-biased, see caveats) ---")
    strong_results = backtest.run_backtest(
        bars, symbols=STRONG_CANDIDATES, directions=DIRECTIONS, grid_pct=GRID_PCT, symbol_atr=symbol_atr, atr_multiplier=backtest.DEFAULT_ATR_MULTIPLIER
    )
    print(backtest.summarize(strong_results, goal_daily_pnl=GOAL_DAILY_PNL))

    print("\n--- Per-symbol isolated comparison (each gets full capital) ---")
    comparison = backtest.per_symbol_comparison(
        bars, symbols=SYMBOLS, directions=DIRECTIONS, grid_pct=GRID_PCT, symbol_atr=symbol_atr, atr_multiplier=backtest.DEFAULT_ATR_MULTIPLIER
    )
    print(comparison.to_string(index=False))

    print(CAVEATS)


if __name__ == "__main__":
    main()
