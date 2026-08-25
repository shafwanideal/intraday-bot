import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import backtest, kite_data

# A standalone test plan -- separate from scripts/run_daily_plan_backtest.py's
# running record of actual picks, per user request. Overlapping dates here
# (Aug 11, Aug 14) are intentionally independent of that other script's plan.
DAILY_PLAN: dict[date, dict[str, str]] = {
    date(2026, 8, 11): {"TMB": "short"},  # Tamilnad Mercantile Bank
    date(2026, 7, 20): {"HDFCBANK": "short"},
    date(2026, 8, 14): {"ADANIPORTS": "long", "MOTISONS": "long"},  # Adani Ports, Motisons Jewellers
}

GRID_PCT = 0.015  # revised from 2% on 2026-08-25
GOAL_DAILY_PNL = 2_000

ALL_SYMBOLS = sorted({sym for day_plan in DAILY_PLAN.values() for sym in day_plan})

CAVEATS = """
CAVEATS:
- Data: Kite Connect historical data API, 5-minute bars.
- Grid trigger levels are evaluated on each 5-min bar's CLOSE, not tick by tick.
- Only ONE averaging leg is modeled: entry + one add-on, not a multi-level grid.
- Leverage modeled at a flat 5x on margin -- verify per-symbol via Kite's
  margin calculator before trading live.
- Transaction costs ARE modeled (brokerage, STT, exchange charges, SEBI
  charges, stamp duty, GST). Slippage is NOT modeled.
- Daily loss cap is checked mark-to-market every 5-min bar, not tick by tick.
- This is a standalone test plan, independent of scripts/run_daily_plan_backtest.py.
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

    print(f"\n{'=' * 70}\nTest plan backtest: {len(DAILY_PLAN)} trading days, grid={GRID_PCT:.0%}, leverage={backtest.LEVERAGE}x\n{'=' * 70}\n")
    print(backtest.summarize(results, goal_daily_pnl=GOAL_DAILY_PNL))

    print("\n--- Day by day ---")
    import pandas as pd

    daily = pd.DataFrame(results["daily_results"]).sort_values("date")
    trades = pd.DataFrame(results["trade_log"])
    for _, row in daily.iterrows():
        day = row["date"]
        symbols_today = ", ".join(f"{s}({d})" for s, d in DAILY_PLAN.get(day, {}).items())
        note = f"  [{row['note']}]" if isinstance(row.get("note"), str) else ""
        print(f"{day}  {symbols_today:45s} net P&L: Rs {row['pnl']:>10,.2f}{note}")
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
