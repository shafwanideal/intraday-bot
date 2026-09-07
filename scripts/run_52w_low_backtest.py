"""Backtest the grid-averaging/2%-trail swing strategy against a choice of
entry trigger (see TRIGGER_SCREENERS): enter whenever a stock's own trigger
condition fires, average down every 3% further fall (unlimited legs, same
share qty per leg), arm a trailing stop once the position is 2% above its
current blended average, then trail using ATR (0.5x 14-day ATR by default).
No square-off -- this is swing/positional (CNC or MTF, not MIS).

Capital per leg, the entry-window length, the universe, and the trigger are
all CLI args (see the DEFAULT_* constants) rather than fixed, since these
get re-run with different real capital plans, universes, and (2026-09-02)
entry triggers for direct comparison.

Usage: python3 scripts/run_52w_low_backtest.py [capital_per_leg] [lookback_trading_days] [universe] [max_total_capital] [trigger] [source]
  universe: nifty50 (default) | nifty200 | nifty500
  max_total_capital: hard cap on TOTAL capital committed across everything at once
    (entries AND averaging legs are refused once this would be exceeded). Omit for
    uncapped (old behavior). Pass "none" to skip it while still setting later args.
  trigger: 52w_low (default) | rsi_dip -- rsi_dip enters on RSI(14)<=30 while price
    is still above its 200-day EMA (a real uptrend), instead of any fresh 52-week low
    regardless of the stock's underlying trend.
  source: yahoo (default) | kite -- where the daily bars come from. Only daily bars
    are needed here, and Yahoo serves those free and unauthenticated, so this runs
    anywhere; kite needs a same-day interactive login (python3 -m src.auth).
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import indicators, kite_data, screener, swing_strategy, yahoo_daily

DEFAULT_CAPITAL_PER_LEG = 200_000
DEFAULT_LOOKBACK_TRADING_DAYS = 60  # window over which NEW entries are allowed
DEFAULT_UNIVERSE = "nifty50"
DEFAULT_TRIGGER = "52w_low"
DEFAULT_SOURCE = "yahoo"
DAILY_HISTORY_DAYS = 850  # covers up to a 1-year (~252 trading day) entry window + the 252-day lookback + buffer
ATR_MULTIPLIER = 0.5
ATR_PERIOD = 14

UNIVERSE_LOADERS = {
    "nifty50": screener.load_nifty50_symbols,
    "nifty200": screener.load_nifty200_symbols,
    "nifty500": screener.load_nifty500_symbols,
}

TRIGGER_SCREENERS = {
    "52w_low": screener.screen_52w_low_entries,
    "rsi_dip": screener.screen_rsi_dip_entries,
}

# (fetch_daily(symbol, days=...) -> DataFrame, inter-request delay seconds)
DATA_SOURCES = {
    "yahoo": (yahoo_daily.fetch_daily, yahoo_daily.REQUEST_DELAY_SECONDS),
    "kite": (kite_data.fetch_daily, kite_data.REQUEST_DELAY_SECONDS),
}

TRIGGER_LABELS = {
    "52w_low": "fresh 52-week low",
    "rsi_dip": "RSI(14)<=30 oversold while above 200-day EMA",
}


def main() -> None:
    capital_per_leg = float(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CAPITAL_PER_LEG
    lookback_trading_days = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_LOOKBACK_TRADING_DAYS
    universe = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_UNIVERSE
    max_total_capital = None
    if len(sys.argv) > 4 and sys.argv[4].lower() not in ("none", "-"):
        max_total_capital = float(sys.argv[4])
    trigger = sys.argv[5] if len(sys.argv) > 5 else DEFAULT_TRIGGER
    source = sys.argv[6] if len(sys.argv) > 6 else DEFAULT_SOURCE
    screen_entries = TRIGGER_SCREENERS[trigger]
    fetch_daily, request_delay = DATA_SOURCES[source]

    symbols = UNIVERSE_LOADERS[universe]()
    print(f"Universe: {len(symbols)} {universe} symbols")

    print(f"Fetching {DAILY_HISTORY_DAYS}d of daily bars for all {len(symbols)} symbols from {source} ...")
    daily_data: dict = {}
    for i, sym in enumerate(symbols):
        df = fetch_daily(sym, days=DAILY_HISTORY_DAYS)
        if not df.empty:
            daily_data[sym] = df
        if (i + 1) % 25 == 0:
            print(f"  ... {i + 1}/{len(symbols)} symbols fetched ({len(daily_data)} with data)")
        time.sleep(request_delay)
    print(f"Got data for {len(daily_data)}/{len(symbols)} symbols")

    all_dates = sorted({d for df in daily_data.values() for d in df.index.date})
    entry_window = all_dates[-lookback_trading_days:]
    print(f"Entry window: {entry_window[0]} to {entry_window[-1]} ({len(entry_window)} trading days)")
    print(f"Positions carried forward through: {all_dates[-1]} (latest available data)")

    engine = swing_strategy.SwingEngine(
        capital_per_leg=capital_per_leg, atr_multiplier=ATR_MULTIPLIER, max_total_capital=max_total_capital
    )
    entry_window_set = set(entry_window)

    # Track total capital deployed across ALL open positions on every date, to find the
    # real peak concurrent capital requirement -- not just what's open at the very end.
    capital_by_date: list[tuple] = []

    total_trigger_events = 0
    skipped_no_capital = 0
    for d in all_dates:
        if d >= entry_window[0]:
            if d in entry_window_set:
                # Ordered by signal strength (deepest breakdown / most oversold first) --
                # matters when capital is capped and not every signal can be taken the same day.
                triggered = screen_entries(daily_data, d)
                total_trigger_events += len(triggered)
                for sym in triggered:
                    if sym in engine.open_positions:
                        continue
                    if not engine.can_enter(sym):
                        skipped_no_capital += 1
                        continue
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

            total_capital_today = sum(
                sum(p * q for p, q, _ in pos.legs) for pos in engine.open_positions.values()
            )
            capital_by_date.append((d, total_capital_today, list(engine.open_positions.keys())))

    print(f"Total trigger events ({TRIGGER_LABELS[trigger]}) in the entry window: {total_trigger_events}")
    if max_total_capital is not None:
        print(f"Signals SKIPPED due to the Rs {max_total_capital:,.0f} capital cap: {skipped_no_capital}")

    trades = engine.closed_trades
    still_open = engine.open_positions

    print(f"\n{'=' * 70}")
    print(f"{trigger} entry backtest ({universe} via {source}, unlimited-leg averaging, ATR trailing)")
    cap_str = f"Rs {max_total_capital:,.0f}" if max_total_capital is not None else "UNCAPPED"
    print(
        f"Capital per leg: Rs {capital_per_leg:,.0f}  Averaging drop: {swing_strategy.AVERAGING_DROP_PCT:.0%}  "
        f"Trail activates: {swing_strategy.PROFIT_TARGET_PCT:.0%}  ATR multiplier: {ATR_MULTIPLIER}x  "
        f"Window: {lookback_trading_days} trading days  Max total capital: {cap_str}"
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

    print(f"\nStill-open positions (STUCK -- never hit +2% and trailing-stopped out): {len(still_open)}")
    total_unrealized = 0.0
    if still_open:
        armed_count = sum(1 for pos in still_open.values() if pos.trailing)
        print(f"  ({armed_count} currently armed/trailing, {len(still_open) - armed_count} still averaging/waiting for +2%)")
        for sym, pos in still_open.items():
            last_close = float(daily_data[sym].iloc[-1]["Close"])
            unrealized = (last_close - pos.avg_price) * pos.qty
            capital = sum(p * q for p, q, _ in pos.legs)
            total_unrealized += unrealized
            trail_tag = " [TRAILING]" if pos.trailing else ""
            print(
                f"  {sym:12s} entered {pos.legs[0][2]}  legs={len(pos.legs)}  avg={pos.avg_price:.2f}{trail_tag}  "
                f"last_close={last_close:.2f}  capital=Rs {capital:>12,.0f}  unrealized=Rs {unrealized:>10,.2f}"
            )

    total_net = sum(t["pnl"] for t in trades) if trades else 0.0
    print(f"\nCOMBINED (realized + unrealized, mark-to-market today): Rs {total_net + total_unrealized:,.2f}")

    if capital_by_date:
        peak_date, peak_capital, peak_symbols = max(capital_by_date, key=lambda x: x[1])
        print(f"\nPEAK concurrent capital deployed: Rs {peak_capital:,.2f} on {peak_date}")
        print(f"  Positions open then: {peak_symbols}")

    # --- Per-stock detailed report ---
    print(f"\n{'=' * 70}\nPER-STOCK REPORT\n{'=' * 70}")
    all_symbols = sorted(set([t["symbol"] for t in trades]) | set(still_open.keys()))
    for sym in all_symbols:
        sym_trades = [t for t in trades if t["symbol"] == sym]
        realized = sum(t["pnl"] for t in sym_trades)
        wins = sum(1 for t in sym_trades if t["pnl"] > 0)
        status = "OPEN" if sym in still_open else "flat"
        print(f"\n{sym} -- {len(sym_trades)} closed round-trip(s), status: {status}")
        if sym_trades:
            print(f"  Realized: Rs {realized:,.2f}  ({wins}/{len(sym_trades)} won)")
            for t in sorted(sym_trades, key=lambda x: x["entry_date"]):
                print(
                    f"    {t['entry_date']} -> {t['exit_date']}  legs={t['legs']}  avg={t['avg_price']:.2f} "
                    f"exit={t['exit_price']:.2f}  net=Rs {t['pnl']:>9,.2f}"
                )
        if sym in still_open:
            pos = still_open[sym]
            last_close = float(daily_data[sym].iloc[-1]["Close"])
            unrealized = (last_close - pos.avg_price) * pos.qty
            capital = sum(p * q for p, q, _ in pos.legs)
            trail_state = "ARMED/TRAILING" if pos.trailing else "still averaging/waiting for +2%"
            print(
                f"  CURRENTLY OPEN ({trail_state}): entered {pos.legs[0][2]}, {len(pos.legs)} leg(s), "
                f"avg={pos.avg_price:.2f}, last_close={last_close:.2f}, capital=Rs {capital:,.0f}, "
                f"unrealized=Rs {unrealized:,.2f}"
            )
            print(f"  Legs: {[(round(p,2), q, str(dt)) for p, q, dt in pos.legs]}")

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
