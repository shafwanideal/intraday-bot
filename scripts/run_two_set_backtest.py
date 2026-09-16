import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import backtest, kite_data

TODAY = date.today()

# Set 1: an absolute rupee amount per stock ("put exactly Rs X into this stock"),
# independent of total margin_capital/leverage -- see backtest.run_backtest's
# "rupees" handling. All four long (no direction given by the user for this set).
SET_1: dict[str, str | dict] = {
    "INFY": {"direction": "long", "rupees": 25_000},
    "TATACHEM": {"direction": "long", "rupees": 25_000},
    "GROWW": {"direction": "long", "rupees": 15_000},
    "KEC": {"direction": "long", "rupees": 15_000},
}

# Set 2: reproduces the symbols/quantities from today's real contract note screenshot.
# Simplification: KEC was actually built up in 3 separate buy legs today (190 @ 427.10,
# 190 @ 411.59, 222 @ 406.64) before one 602-share sell -- our engine models one entry
# per symbol (averaging is off per the 2026-09-13 spec), so this uses the NET position
# (602 shares, long) rather than replaying each tranche. All four are long (buy leg came
# before the sell leg for every symbol in the screenshot).
SET_2: dict[str, str | dict] = {
    "RELIANCE": {"direction": "long", "qty": 1},
    "KEC": {"direction": "long", "qty": 602},
    "INFY": {"direction": "long", "qty": 125},
    "GROWW": {"direction": "long", "qty": 377},
}

GRID_PCT = 0.015
GOAL_DAILY_PNL = 2_000

CAVEATS = """
CAVEATS (read before acting on these numbers):
- Data: Kite Connect historical data API, 5-minute bars. Requires a same-day
  Kite login (python3 -m src.auth).
- Simulates ONLY today's date. If today's session hasn't happened yet or
  Kite has no data for it, this reports "no data for this day's symbol(s)".
- Set 1 uses absolute rupee amounts per stock (Rs 25k/25k/15k/15k), NOT a
  percentage of margin capital -- quantity = rupees / entry price, no
  leverage applied. This is independent of your actual account balance.
- Set 2 uses the exact share quantities from today's contract note
  screenshot (RELIANCE 1, KEC 602, INFY 125, GROWW 377), all long. KEC's
  real 3-tranche buildup (190+190+222 shares at three different prices) is
  simplified to a single 602-share entry at the day's real open price --
  our engine models one entry per symbol, not three separate buys.
- Both sets enter at today's real market open (9:15 AM) -- NOT via the new
  pre-open (9:00 AM) order path, which only exists in live trading
  (src/live.py), not in this backtester.
- No averaging leg -- enable_averaging=False, one entry per symbol.
- A trailing stop arms once a position moves GRID_PCT (1.5%) in its favor,
  then trails 0.75% behind the best price seen.
- No leverage (1x) -- exposure is the literal rupees/qty you specify.
- Transaction costs ARE modeled (brokerage, STT, exchange charges, SEBI
  charges, stamp duty, GST). Slippage is NOT modeled.
- This replays today's REAL price action against a hypothetical entry --
  it does not mean either set was actually traded this way.
"""


def run_one(label: str, plan: dict[str, str | dict]) -> None:
    symbols = sorted(plan.keys())
    print(f"\n{label}:")
    for sym, entry in plan.items():
        direction = entry["direction"]
        size = f"Rs {entry['rupees']:,.0f}" if "rupees" in entry else f"{entry['qty']} shares"
        print(f"  {sym:10s} {direction:5s} {size}")

    print(f"Fetching intraday data for {symbols} from Kite (this re-authenticates if needed) ...")
    bars = kite_data.fetch_many(symbols, days=5, interval="5minute")
    if not bars:
        print("No data fetched for any symbol. Aborting this set.")
        return

    symbol_atr = kite_data.fetch_symbol_atr(symbols)

    results = backtest.run_backtest(
        bars,
        daily_plan={TODAY: plan},
        grid_pct=GRID_PCT,
        symbol_atr=symbol_atr,
        atr_multiplier=backtest.DEFAULT_ATR_MULTIPLIER,
        enable_averaging=False,
    )

    print(f"\n{'=' * 70}\n{label}: {TODAY}, grid={GRID_PCT:.0%}, leverage={backtest.LEVERAGE}x\n{'=' * 70}\n")
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


def main() -> None:
    run_one("SET 1 (rupee-sized: INFY/TATACHEM 25k, GROWW/KEC 15k)", SET_1)
    run_one("SET 2 (matches today's contract note quantities)", SET_2)
    print(CAVEATS)


if __name__ == "__main__":
    main()
