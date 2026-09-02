"""Backtest requested 2026-09-02: Nifty 50 only (large cap), enter whenever
a stock prints a FRESH 52-week low, average down every 3% further fall
(unlimited legs, same share qty per leg), arm a trailing stop once the
position is 2% above its current blended average, then trail using ATR
(0.5x 14-day ATR by default -- same multiplier the intraday strategy
uses, since no multiplier was specified here either). No square-off --
this is swing/positional (CNC or MTF, not MIS).

Capital per leg: Rs 2,00,000, matching the user's stated large-cap MTF
plan (Rs 50,000 own capital x ~4x MTF).
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import indicators, kite_data, screener, swing_strategy

LOOKBACK_TRADING_DAYS = 60  # window over which NEW entries (fresh 52w lows) are allowed
DAILY_HISTORY_DAYS = 500
ATR_MULTIPLIER = 0.5
ATR_PERIOD = 14


def main() -> None:
    symbols = screener.load_nifty50_symbols()
    print(f"Universe: {len(symbols)} Nifty 50 (large cap) symbols")

    print(f"Fetching {DAILY_HISTORY_DAYS}d of daily bars for all {len(symbols)} symbols ...")
    daily_data: dict = {}
    for i, sym in enumerate(symbols):
        df = kite_data.fetch_daily(sym, days=DAILY_HISTORY_DAYS)
        if not df.empty:
            daily_data[sym] = df
        time.sleep(kite_data.REQUEST_DELAY_SECONDS)
    print(f"Got data for {len(daily_data)}/{len(symbols)} symbols")

    all_dates = sorted({d for df in daily_data.values() for d in df.index.date})
    entry_window = all_dates[-LOOKBACK_TRADING_DAYS:]
    print(f"Entry window: {entry_window[0]} to {entry_window[-1]} ({len(entry_window)} trading days)")
    print(f"Positions carried forward through: {all_dates[-1]} (latest available data)")

    engine = swing_strategy.SwingEngine(atr_multiplier=ATR_MULTIPLIER)
    entry_window_set = set(entry_window)

    fifty_two_week_low_events = 0
    for d in all_dates:
        if d >= entry_window[0]:
            if d in entry_window_set:
                triggered = screener.screen_52w_low_entries(daily_data, d)
                fifty_two_week_low_events += len(triggered)
                for sym in triggered:
                    if engine.can_enter(sym):
                        day_rows = daily_data[sym][daily_data[sym].index.date == d]
                        if not day_rows.empty:
                            atr = indicators.atr(daily_data[sym][daily_data[sym].index.date < d], period=ATR_PERIOD)
                            engine.enter(sym, float(day_rows.iloc[0]["Close"]), d, atr=atr)

            for sym in list(engine.open_positions.keys()):
                if sym not in daily_data:
                    continue
                day_rows = daily_data[sym][daily_data[sym].index.date == d]
                if day_rows.empty:
                    continue
                engine.update(sym, float(day_rows.iloc[0]["Close"]), d)

    print(f"Total fresh-52w-low events in the entry window: {fifty_two_week_low_events}")

    trades = engine.closed_trades
    still_open = engine.open_positions

    print(f"\n{'=' * 70}")
    print("52-week-low entry backtest (Nifty 50, unlimited-leg averaging, ATR trailing)")
    print(
        f"Capital per leg: Rs {swing_strategy.CAPITAL_PER_LEG:,}  Averaging drop: {swing_strategy.AVERAGING_DROP_PCT:.0%}  "
        f"Trail activates: {swing_strategy.PROFIT_TARGET_PCT:.0%}  ATR multiplier: {ATR_MULTIPLIER}x"
    )
    print(f"{'=' * 70}\n")

    print(f"Closed (trailing-stopped out after arming) trades: {len(trades)}")
    if trades:
        total_net = sum(t["pnl"] for t in trades)
        wins = [t for t in trades if t["pnl"] > 0]
        avg_legs = sum(t["legs"] for t in trades) / len(trades)
        avg_hold_days = sum((t["exit_date"] - t["entry_date"]).days for t in trades) / len(trades)
        print(f"Total realized net P&L: Rs {total_net:,.2f}")
        print(f"Win rate: {len(wins)}/{len(trades)} ({100*len(wins)/len(trades):.1f}%)")
        print(f"Avg legs per closed trade: {avg_legs:.1f}  Avg holding period: {avg_hold_days:.1f} calendar days")
        print("\n--- Closed trades ---")
        for t in sorted(trades, key=lambda x: x["entry_date"]):
            print(
                f"{t['symbol']:12s} {t['entry_date']} -> {t['exit_date']}  legs={t['legs']}  "
                f"avg={t['avg_price']:.2f} exit={t['exit_price']:.2f}  capital=Rs {t['capital_deployed']:>10,.0f}  "
                f"net=Rs {t['pnl']:>9,.2f}"
            )

    print(f"\nStill-open positions: {len(still_open)}")
    if still_open:
        total_unrealized = 0.0
        total_capital_committed = 0.0
        armed_count = sum(1 for pos in still_open.values() if pos.trailing)
        print(f"  ({armed_count} armed/trailing, {len(still_open) - armed_count} still averaging/waiting)")
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

    total_net = sum(t["pnl"] for t in trades) if trades else 0.0
    total_unrealized = sum(
        (float(daily_data[sym].iloc[-1]["Close"]) - pos.avg_price) * pos.qty for sym, pos in still_open.items()
    )
    print(f"\nCOMBINED (realized + unrealized, mark-to-market today): Rs {total_net + total_unrealized:,.2f}")

    print(
        "\nCAVEATS:\n"
        "- Entry/averaging/trailing checks use DAILY CLOSE (fresh-52w-low check uses that day's\n"
        "  Low), not intraday ticks -- appropriate for a swing strategy, but real fills would\n"
        "  differ slightly.\n"
        "- No stop-loss and no cap on averaging legs, no cap on how many stocks can enter on the\n"
        "  same day -- a real, open-ended capital-commitment risk, worse in a broad market\n"
        "  selloff where many Nifty 50 names could hit fresh lows simultaneously.\n"
        "- Real delivery (CNC) charges modeled -- NOT MTF interest, which would make real\n"
        "  results worse than shown here for any position held on margin.\n"
        "- ATR computed once at entry (14-day, using data before the entry day) and held fixed\n"
        "  for that position's whole life, same convention as the intraday engine."
    )


if __name__ == "__main__":
    main()
