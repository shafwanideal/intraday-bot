import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import backtest, kite_data

# Ad-hoc watchlist requested 2026-09-04: BSE/Ceigall long, Gland Pharma/Tata
# Chemicals short. Symbol spellings: "Ceigal" -> CEIGALL (Ceigall India),
# "Gland Pharma" -> GLAND, "Tata Chemical" -> TATACHEM. Verify these against
# Kite's instrument list (kite.instruments("NSE")) before trusting them --
# a typo'd tradingsymbol just gets silently skipped by kite_data.fetch_many.
SYMBOLS = ["BSE", "CEIGALL", "GLAND", "TATACHEM"]
DIRECTIONS: dict[str, str] = {
    "BSE": "long",
    "CEIGALL": "long",
    "GLAND": "short",
    "TATACHEM": "short",
}
GRID_PCT = 0.015  # matches other scripts in this repo, revised from 2% on 2026-08-25
GOAL_DAILY_PNL = 2_000

CAVEATS = """
CAVEATS (read before acting on these numbers):
- Data: Kite Connect historical data API, 5-minute bars, last ~180 calendar
  days. Requires a same-day Kite login (python3 -m src.auth) -- this cannot
  run in a non-interactive/sandboxed session with no cached access token.
- These 4 symbols trade together in ONE portfolio backtest (3-slot / 4-unit
  pool, entries in SYMBOLS list order) -- if all 4 would qualify to enter on
  the same day, one is left out. The per-symbol comparison table below shows
  each symbol in isolation with full capital, to separate "this stock is a
  loser" from "this stock lost its slot to another one."
- Directions (long/short) are applied on EVERY trading day in the fetched
  window, not just one specific date -- there was no date attached to this
  request. If this was meant as a single day's pick, re-run
  scripts/run_daily_plan_backtest.py with today's date and these symbols
  instead.
- Grid trigger levels are evaluated on each 5-min bar's CLOSE, not tick by
  tick.
- Only ONE averaging leg is modeled: entry + one add-on, not a multi-level
  grid.
- Leverage modeled at a flat 5x on margin -- verify actual Zerodha MIS
  leverage per symbol via Kite's margin calculator before trading live.
- Transaction costs ARE modeled (brokerage, STT, exchange charges, SEBI
  charges, stamp duty, GST) on the leveraged order value. Slippage is NOT
  modeled.
- CEIGALL (Ceigall India) IPO'd in 2024 -- fewer days of trading history
  exist than for established large-caps, so its sample size here will be
  smaller and less reliable than BSE/GLAND/TATACHEM.
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

    print("\n--- Portfolio (3 slots / 4 units, entries in list order: BSE, CEIGALL, GLAND, TATACHEM) ---")
    portfolio_results = backtest.run_backtest(
        bars, symbols=SYMBOLS, directions=DIRECTIONS, grid_pct=GRID_PCT, symbol_atr=symbol_atr, atr_multiplier=backtest.DEFAULT_ATR_MULTIPLIER
    )
    print(backtest.summarize(portfolio_results, goal_daily_pnl=GOAL_DAILY_PNL))

    print("\n--- Per-symbol isolated comparison (each gets full capital) ---")
    comparison = backtest.per_symbol_comparison(
        bars, symbols=SYMBOLS, directions=DIRECTIONS, grid_pct=GRID_PCT, symbol_atr=symbol_atr, atr_multiplier=backtest.DEFAULT_ATR_MULTIPLIER
    )
    print(comparison.to_string(index=False))

    print(CAVEATS)


if __name__ == "__main__":
    main()
