import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import backtest, kite_data

# The user's actual morning-of stock picks, as given, one entry per (date, symbol).
DAILY_PLAN: dict[date, dict[str, str]] = {
    date(2026, 8, 7): {"KALYANKJIL": "long"},  # Kalyan Jewellers
    date(2026, 8, 10): {"AARTIPHARM": "long"},  # Aarti Pharmalabs
    date(2026, 8, 11): {"BSE": "long", "THYROCARE": "long", "MAHABANK": "long"},  # BSE, Thyrocare, Bank of Maharashtra
    date(2026, 8, 12): {"LENSKART": "long"},
    date(2026, 8, 14): {"LGEINDIA": "long", "WELSPUNLIV": "long", "MANORAMA": "long"},  # LG Electronics India, Welspun Living, Manorama Industries
    date(2026, 8, 25): {"IIFL": "short", "HINDCOPPER": "short", "MFSL": "long"},  # IIFL Finance, Hindustan Copper, Max Financial Services
}

GRID_PCT = 0.02  # established as the better setting net of costs
GOAL_DAILY_PNL = 2_000

ALL_SYMBOLS = sorted({sym for day_plan in DAILY_PLAN.values() for sym in day_plan})

CAVEATS = """
CAVEATS:
- Data: Kite Connect historical data API, 5-minute bars. Same feed as live
  quotes/shadow mode -- no cross-source mismatch.
- Grid trigger levels are evaluated on each 5-min bar's CLOSE, not tick by
  tick.
- Only ONE averaging leg is modeled: entry + one add-on, not a multi-level
  grid.
- Leverage modeled at a flat 5x on margin -- real Zerodha MIS leverage
  varies per stock, verify via Kite's margin calculator before trading live.
- Transaction costs ARE modeled (brokerage, STT, exchange charges, SEBI
  charges, stamp duty, GST) on the leveraged order value. Slippage is NOT
  modeled.
- Daily loss cap is checked mark-to-market every 5-min bar, not tick by
  tick -- a live system would enforce it more tightly.
- Some of these are very recent/thin listings (Lenskart, TMPV) -- fewer
  days of trading history exist, and liquidity/spread risk may be higher
  than for established large-caps.
"""


def main() -> None:
    print(f"Fetching intraday data for {ALL_SYMBOLS} from Kite ...")
    bars = kite_data.fetch_many(ALL_SYMBOLS, days=180, interval="5minute")

    missing = [s for s in ALL_SYMBOLS if s not in bars]
    if missing:
        print(f"WARNING: no data for {missing}")

    print("Fetching ATR (14d) for volatility-scaled trailing stop ...")
    symbol_atr = kite_data.fetch_symbol_atr(ALL_SYMBOLS)

    results = backtest.run_backtest(
        bars, daily_plan=DAILY_PLAN, grid_pct=GRID_PCT, symbol_atr=symbol_atr, atr_multiplier=backtest.DEFAULT_ATR_MULTIPLIER
    )

    print(f"\n{'=' * 70}\nDaily-plan backtest: {len(DAILY_PLAN)} trading days, grid={GRID_PCT:.0%}, leverage={backtest.LEVERAGE}x\n{'=' * 70}\n")

    print(backtest.summarize(results, goal_daily_pnl=GOAL_DAILY_PNL))

    print("\n--- Day by day ---")
    import pandas as pd

    daily = pd.DataFrame(results["daily_results"]).sort_values("date")
    trades = pd.DataFrame(results["trade_log"])
    for _, row in daily.iterrows():
        day = row["date"]
        symbols_today = ", ".join(f"{s}({d})" for s, d in DAILY_PLAN.get(day, {}).items())
        note = f"  [{row['note']}]" if isinstance(row.get("note"), str) else ""
        print(f"{day}  {symbols_today:55s} net P&L: Rs {row['pnl']:>10,.2f}{note}")
        if not trades.empty:
            day_trades = trades[trades["date"] == day]
            for _, tr in day_trades.iterrows():
                print(
                    f"    {tr['symbol']:12s} {tr['direction']:5s} avg={tr['avg_price']:.2f} "
                    f"exit={tr['exit_price']:.2f} qty={tr['qty']:.1f} units={tr['units_used']} "
                    f"gross={tr['gross_pnl']:>9,.2f} costs={tr['costs']:>6,.2f} net={tr['pnl']:>9,.2f} ({tr['reason']})"
                )

    print(CAVEATS)


if __name__ == "__main__":
    main()
