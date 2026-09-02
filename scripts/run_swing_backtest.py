"""Backtest the swing/positional averaging strategy requested 2026-09-02:
unlimited-leg averaging (fixed share qty per leg) every 3% fall from the
last fill, exit the WHOLE position at 2% above the current blended
average, no square-off -- positions can span many days. Reuses the same
Groww-breakout screener (src/screener.py) for entries: the last 60 trading
days are screened (no lookahead), and a NEW swing position opens for any
top-3-by-volume pick not already held.

Positions can still be open past the 60-day entry window (this is swing,
not intraday) -- they're carried forward on daily closes up through the
latest available data and reported as still-open/unrealized if the target
was never hit.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import kite_data, screener, swing_strategy

LOOKBACK_TRADING_DAYS = 60  # window over which NEW entries are allowed
TOP_N = 3
DAILY_HISTORY_DAYS = 500


def main() -> None:
    symbols = screener.load_nifty200_symbols()
    print(f"Universe: {len(symbols)} Nifty 200 symbols")

    print(f"Fetching {DAILY_HISTORY_DAYS}d of daily bars for all {len(symbols)} symbols ...")
    daily_data: dict = {}
    for i, sym in enumerate(symbols):
        df = kite_data.fetch_daily(sym, days=DAILY_HISTORY_DAYS)
        if not df.empty:
            daily_data[sym] = df
        if (i + 1) % 50 == 0:
            print(f"  ... {i + 1}/{len(symbols)}")
        time.sleep(kite_data.REQUEST_DELAY_SECONDS)

    all_dates = sorted({d for df in daily_data.values() for d in df.index.date})
    entry_window = all_dates[-LOOKBACK_TRADING_DAYS:]
    print(f"Entry window: {entry_window[0]} to {entry_window[-1]} ({len(entry_window)} trading days)")
    print(f"Positions carried forward through: {all_dates[-1]} (latest available data)")

    engine = swing_strategy.SwingEngine()
    entry_window_set = set(entry_window)
    forward_dates = [d for d in all_dates if d >= entry_window[0]]

    for d in forward_dates:
        if d in entry_window_set:
            picks = screener.screen_day(daily_data, d, top_n=TOP_N)
            for sym in picks:
                if engine.can_enter(sym) and sym in daily_data:
                    day_rows = daily_data[sym][daily_data[sym].index.date == d]
                    if not day_rows.empty:
                        engine.enter(sym, float(day_rows.iloc[0]["Close"]), d)

        for sym in list(engine.open_positions.keys()):
            if sym not in daily_data:
                continue
            day_rows = daily_data[sym][daily_data[sym].index.date == d]
            if day_rows.empty:
                continue
            engine.update(sym, float(day_rows.iloc[0]["Close"]), d)

    trades = engine.closed_trades
    still_open = engine.open_positions

    print(f"\n{'=' * 70}")
    print("Swing-averaging backtest (unlimited legs, 3% add / 2% arms trailing stop, no square-off)")
    print(
        f"Capital per leg: Rs {swing_strategy.CAPITAL_PER_LEG:,}  Averaging drop: {swing_strategy.AVERAGING_DROP_PCT:.0%}  "
        f"Trail activates: {swing_strategy.PROFIT_TARGET_PCT:.0%}  Trail distance: {engine.trailing_pct:.0%}"
    )
    print(f"{'=' * 70}\n")

    print(f"Closed (trailing-stopped out after arming) trades: {len(trades)}")
    if trades:
        total_net = sum(t["pnl"] for t in trades)
        wins = [t for t in trades if t["pnl"] > 0]
        print(f"Total realized net P&L: Rs {total_net:,.2f}")
        print(f"Win rate: {len(wins)}/{len(trades)} ({100*len(wins)/len(trades):.1f}%)")
        avg_legs = sum(t["legs"] for t in trades) / len(trades)
        avg_hold_days = sum((t["exit_date"] - t["entry_date"]).days for t in trades) / len(trades)
        max_capital = max(t["capital_deployed"] for t in trades)
        print(f"Avg legs per closed trade: {avg_legs:.1f}  Avg holding period: {avg_hold_days:.1f} calendar days")
        print(f"Largest capital deployed on one (closed) position: Rs {max_capital:,.2f}")
        print("\n--- Closed trades ---")
        for t in sorted(trades, key=lambda x: x["entry_date"]):
            print(
                f"{t['symbol']:12s} {t['entry_date']} -> {t['exit_date']}  legs={t['legs']}  "
                f"avg={t['avg_price']:.2f} exit={t['exit_price']:.2f}  capital=Rs {t['capital_deployed']:>10,.0f}  "
                f"net=Rs {t['pnl']:>9,.2f}"
            )

    print(f"\nStill-open positions (never trailing-stopped out by {all_dates[-1]}): {len(still_open)}")
    if still_open:
        total_unrealized = 0.0
        total_capital_committed = 0.0
        armed_count = sum(1 for pos in still_open.values() if pos.trailing)
        print(f"  ({armed_count} armed/trailing -- have hit +2% at some point; {len(still_open) - armed_count} never yet reached it)")
        for sym, pos in still_open.items():
            last_close = float(daily_data[sym].iloc[-1]["Close"])
            unrealized = (last_close - pos.avg_price) * pos.qty
            capital = sum(p * q for p, q, _ in pos.legs)
            total_unrealized += unrealized
            total_capital_committed += capital
            trail_tag = f" [TRAILING, peak={pos.peak_price:.2f}]" if pos.trailing else ""
            print(
                f"  {sym:12s} entered {pos.legs[0][2]}  legs={len(pos.legs)}  avg={pos.avg_price:.2f}{trail_tag}  "
                f"last_close={last_close:.2f}  capital=Rs {capital:>10,.0f}  unrealized=Rs {unrealized:>9,.2f}"
            )
        print(f"\nTotal capital still committed (open positions): Rs {total_capital_committed:,.2f}")
        print(f"Total unrealized P&L (open positions, mark-to-market): Rs {total_unrealized:,.2f}")

    print(
        "\nCAVEATS:\n"
        "- Entries/averaging/target checks all use DAILY CLOSE prices, not intraday ticks --\n"
        "  appropriate granularity for a multi-day swing strategy, but a real order would fill\n"
        "  at whatever price it hit intraday, not exactly the day's close.\n"
        "- No stop-loss and no cap on the number of averaging legs, exactly as specified --\n"
        "  a real, open-ended capital-commitment risk on any stock that keeps falling.\n"
        "- Real delivery (CNC) charges modeled: zero brokerage, STT both sides, DP charge on sell.\n"
        "- Entries only allowed in the last 60 trading days; positions may still be open today."
    )


if __name__ == "__main__":
    main()
