"""Run every entry trigger against the SAME universe, window and capital plan,
and print them side by side.

This is the comparison that motivated adding the rsi_dip trigger (2026-09-02)
but was never actually run: that trigger was written because the 52w_low
trigger's worst positions were stocks in genuine structural decline, and the
claim that RSI-oversold-while-above-the-200-EMA avoids them needed testing, not
asserting.

Both arms share one fetch of the daily bars and one simulation loop
(`src.swing_backtest.run`), so any difference in the results is the trigger and
nothing else.

Usage: python3 scripts/compare_entry_triggers.py [capital_per_leg] [lookback_trading_days] [universe] [max_total_capital]
  universe: nifty50 (default) | nifty200 | nifty500
  max_total_capital: hard cap on total capital committed at once; "none" for uncapped.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import screener, swing_backtest, swing_strategy, yahoo_daily

DEFAULT_CAPITAL_PER_LEG = 200_000
DEFAULT_LOOKBACK_TRADING_DAYS = 252
DEFAULT_UNIVERSE = "nifty50"
DEFAULT_MAX_TOTAL_CAPITAL = 1_500_000
DAILY_HISTORY_DAYS = 850
ATR_MULTIPLIER = 0.5

UNIVERSE_LOADERS = {
    "nifty50": screener.load_nifty50_symbols,
    "nifty200": screener.load_nifty200_symbols,
    "nifty500": screener.load_nifty500_symbols,
}

TRIGGERS = {
    "52w_low": (screener.screen_52w_low_entries, "fresh 52-week low"),
    "rsi_dip": (screener.screen_rsi_dip_entries, "RSI(14)<=30 while above 200-day EMA"),
}


def _rupees(x: float) -> str:
    return f"Rs {x:>14,.0f}"


def main() -> None:
    capital_per_leg = float(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CAPITAL_PER_LEG
    lookback_trading_days = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_LOOKBACK_TRADING_DAYS
    universe = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_UNIVERSE
    max_total_capital: float | None = DEFAULT_MAX_TOTAL_CAPITAL
    if len(sys.argv) > 4:
        max_total_capital = None if sys.argv[4].lower() in ("none", "-") else float(sys.argv[4])

    symbols = UNIVERSE_LOADERS[universe]()
    print(f"Universe: {len(symbols)} {universe} symbols")
    print(f"Fetching {DAILY_HISTORY_DAYS}d of daily bars (Yahoo, cached per day) ...")
    daily_data = yahoo_daily.fetch_daily_many(symbols, days=DAILY_HISTORY_DAYS)
    print(f"Got data for {len(daily_data)}/{len(symbols)} symbols")

    all_dates = swing_backtest.trading_dates(daily_data)
    entry_window = all_dates[-lookback_trading_days:]
    print(f"Entry window: {entry_window[0]} to {entry_window[-1]} ({len(entry_window)} trading days)")
    print(f"Positions carried forward through: {all_dates[-1]}\n")

    results = {}
    for name, (screen_fn, _) in TRIGGERS.items():
        print(f"Simulating {name} ...")
        result = swing_backtest.run(
            daily_data,
            screen_fn,
            entry_window,
            capital_per_leg=capital_per_leg,
            atr_multiplier=ATR_MULTIPLIER,
            max_total_capital=max_total_capital,
        )
        results[name] = (result, swing_backtest.summarize(result, daily_data))

    cap_str = f"Rs {max_total_capital:,.0f}" if max_total_capital is not None else "UNCAPPED"
    print(f"\n{'=' * 78}")
    print(f"ENTRY TRIGGER COMPARISON -- {universe}, {len(entry_window)} trading-day entry window")
    print(
        f"Capital/leg: Rs {capital_per_leg:,.0f}  Averaging: {swing_strategy.AVERAGING_DROP_PCT:.0%} drop  "
        f"Trail arms: +{swing_strategy.PROFIT_TARGET_PCT:.0%}  ATR mult: {ATR_MULTIPLIER}x  Total cap: {cap_str}"
    )
    print(f"{'=' * 78}\n")

    names = list(TRIGGERS)
    width = 20

    def row(label: str, fn) -> None:
        cells = "".join(f"{fn(results[n]):>{width}}" for n in names)
        print(f"{label:<34}{cells}")

    header = "".join(f"{n:>{width}}" for n in names)
    print(f"{'':<34}{header}")
    print("-" * (34 + width * len(names)))
    row("Distinct symbols triggered", lambda r: f"{len(r[0]['symbols_triggered'])}")
    row("Trigger-days (incl. repeats)", lambda r: f"{r[0]['total_trigger_events']}")
    row("Positions opened", lambda r: f"{r[0]['entries_taken']}")
    row("Refused by capital cap (never in)", lambda r: f"{len(r[0]['never_entered'])}")
    print()
    row("Closed trades", lambda r: f"{r[1]['closed_trades']}")
    row("Win rate", lambda r: f"{r[1]['win_rate'] * 100:.0f}%" if r[1]["closed_trades"] else "-")
    row("Avg legs / closed trade", lambda r: f"{r[1]['avg_legs']:.1f}")
    row("Avg hold (calendar days)", lambda r: f"{r[1]['avg_hold_days']:.0f}")
    print()
    row("Realized P&L", lambda r: _rupees(r[1]["realized"]))
    row("Unrealized (open, mark-to-market)", lambda r: _rupees(r[1]["unrealized"]))
    row("COMBINED", lambda r: _rupees(r[1]["combined"]))
    print()
    row("Still open at end", lambda r: f"{r[1]['still_open']}")
    row("  of those, stuck (never armed)", lambda r: f"{r[1]['stuck']}")
    row("Capital tied up in open positions", lambda r: _rupees(r[1]["open_capital"]))
    row("Peak concurrent capital", lambda r: _rupees(r[1]["peak_capital"]))
    row("Worst open position", lambda r: (f"{r[1]['worst_open'][0]} {r[1]['worst_open'][1]:,.0f}" if r[1]["worst_open"] else "-"))
    row("Biggest single win", lambda r: _rupees(r[1]["biggest_win"]))

    for name in names:
        result, summary = results[name]
        print(f"\n--- {name}: {TRIGGERS[name][1]} ---")
        for t in sorted(result["trades"], key=lambda x: x["entry_date"]):
            print(
                f"  {t['symbol']:12s} {t['entry_date']} -> {t['exit_date']}  legs={t['legs']:2d}  "
                f"avg={t['avg_price']:9.2f} exit={t['exit_price']:9.2f}  net=Rs {t['pnl']:>11,.0f}"
            )
        for sym, pos in result["still_open"].items():
            last_close = float(daily_data[sym].iloc[-1]["Close"])
            state = "ARMED/TRAILING" if pos.trailing else "STUCK (never hit +2%)"
            print(
                f"  {sym:12s} {pos.legs[0][2]} -> OPEN       legs={len(pos.legs):2d}  "
                f"avg={pos.avg_price:9.2f} last={last_close:9.2f}  "
                f"unrl=Rs {(last_close - pos.avg_price) * pos.qty:>11,.0f}  [{state}]"
            )

    print(
        "\nHOW TO READ THIS:\n"
        "- COMBINED is the number that matters. Realized P&L alone flatters both triggers,\n"
        "  because this strategy has no stop-loss: a losing position is never closed, it just\n"
        "  stays open and averages down, so it never enters the realized column at all.\n"
        "- 'Stuck' positions are the real risk: capital committed indefinitely in something\n"
        "  that never reached +2%, with more legs added every 3% it falls.\n"
        "- Check 'Biggest single win' against realized P&L. If one trade is most of the total,\n"
        "  the trigger has not been shown to work -- that is one outcome, not an edge.\n"
        "- Entries and averaging legs fill at the DAILY CLOSE; real fills would differ.\n"
        "- Delivery (CNC) charges are modeled; MTF interest is NOT, so anything held on\n"
        "  margin would do worse than shown.\n"
        "- Index membership is a current snapshot, so this has survivorship bias: today's\n"
        "  Nifty constituents are, by construction, the ones that did not collapse."
    )


if __name__ == "__main__":
    main()
