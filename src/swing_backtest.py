"""The entry-trigger simulation loop, shared by the single-trigger backtest
(`scripts/run_52w_low_backtest.py`) and the side-by-side trigger comparison
(`scripts/compare_entry_triggers.py`).

Extracted so the two cannot drift apart: a comparison whose two arms don't run
byte-identical mechanics isn't a comparison. Everything specific to a trigger
lives in the `screen_entries` callable passed in; everything specific to
position mechanics lives in `src.swing_strategy.SwingEngine`.
"""

from datetime import date
from typing import Callable

import pandas as pd

from . import indicators, swing_strategy

ATR_PERIOD = 14

# A screener: (daily_data, as_of) -> symbols triggered that day, strongest first.
ScreenFn = Callable[[dict[str, pd.DataFrame], date], list[str]]


def trading_dates(daily_data: dict[str, pd.DataFrame]) -> list[date]:
    return sorted({d for df in daily_data.values() for d in df.index.date})


def run(
    daily_data: dict[str, pd.DataFrame],
    screen_entries: ScreenFn,
    entry_window: list[date],
    capital_per_leg: float,
    atr_multiplier: float,
    max_total_capital: float | None = None,
) -> dict:
    """Simulate one entry trigger over `daily_data`.

    New positions are only opened on dates in `entry_window`; positions already
    open keep being managed (averaged, armed, trailed out) on every later date
    in the data, so a trade opened on the last day of the window is still
    followed to its exit rather than being cut off there.

    Entries and averaging legs both fill at that day's CLOSE. Returns the
    engine plus the counters the reports need.
    """
    engine = swing_strategy.SwingEngine(
        capital_per_leg=capital_per_leg,
        atr_multiplier=atr_multiplier,
        max_total_capital=max_total_capital,
    )
    entry_window_set = set(entry_window)
    all_dates = trading_dates(daily_data)

    # Distinguish trigger-DAYS from distinct opportunities: a stock printing a fresh
    # low (or staying oversold) for ten sessions running fires the screen on all ten,
    # but it is one opportunity, and after the first entry the rest are no-ops because
    # the symbol is already open. Counting only trigger-days makes the capital cap look
    # far more binding than it is.
    total_trigger_events = 0  # raw trigger-days, repeats on the same symbol included
    symbols_triggered: set[str] = set()
    skipped_no_capital = 0  # raw refusal-days, same repeat caveat
    symbols_skipped_no_capital: set[str] = set()
    entries_taken = 0
    # Total capital deployed across ALL open positions on every date, to find the real
    # peak concurrent requirement -- not just what happens to be open at the very end.
    capital_by_date: list[tuple[date, float, list[str]]] = []

    for d in all_dates:
        if d < entry_window[0]:
            continue

        if d in entry_window_set:
            # Ordered by signal strength (deepest breakdown / most oversold first) --
            # matters when capital is capped and not every signal can be taken the same day.
            triggered = screen_entries(daily_data, d)
            total_trigger_events += len(triggered)
            symbols_triggered.update(triggered)
            for sym in triggered:
                if sym in engine.open_positions:
                    continue
                if not engine.can_enter(sym):
                    skipped_no_capital += 1
                    symbols_skipped_no_capital.add(sym)
                    continue
                day_rows = daily_data[sym][daily_data[sym].index.date == d]
                if day_rows.empty:
                    continue
                atr = indicators.atr(daily_data[sym][daily_data[sym].index.date < d], period=ATR_PERIOD)
                if engine.enter(sym, float(day_rows.iloc[0]["Close"]), d, atr=atr):
                    entries_taken += 1

        for sym in list(engine.open_positions.keys()):
            if sym not in daily_data:
                continue
            day_rows = daily_data[sym][daily_data[sym].index.date == d]
            if day_rows.empty:
                continue
            engine.update(sym, float(day_rows.iloc[0]["Close"]), d)

        capital_by_date.append(
            (
                d,
                sum(sum(p * q for p, q, _ in pos.legs) for pos in engine.open_positions.values()),
                list(engine.open_positions.keys()),
            )
        )

    # A symbol refused by the cap may still have entered later, once other positions
    # closed and freed capital -- "never entered" is the number that actually cost
    # anything.
    entered = {t["symbol"] for t in engine.closed_trades} | set(engine.open_positions)
    never_entered = sorted(symbols_skipped_no_capital - entered)

    return {
        "engine": engine,
        "trades": engine.closed_trades,
        "still_open": engine.open_positions,
        "total_trigger_events": total_trigger_events,
        "symbols_triggered": symbols_triggered,
        "entries_taken": entries_taken,
        "skipped_no_capital": skipped_no_capital,
        "symbols_skipped_no_capital": symbols_skipped_no_capital,
        "never_entered": never_entered,
        "capital_by_date": capital_by_date,
    }


def summarize(result: dict, daily_data: dict[str, pd.DataFrame]) -> dict:
    """Headline numbers for one trigger's run.

    `combined` (realized + open-position mark-to-market) is the number to judge a
    trigger on: realized P&L alone flatters any strategy that never stops out,
    because every loser stays open and simply doesn't get counted.
    """
    trades = result["trades"]
    still_open = result["still_open"]

    realized = sum(t["pnl"] for t in trades)
    wins = [t for t in trades if t["pnl"] > 0]

    unrealized = 0.0
    open_capital = 0.0
    worst_open = None
    for sym, pos in still_open.items():
        last_close = float(daily_data[sym].iloc[-1]["Close"])
        pnl = (last_close - pos.avg_price) * pos.qty
        unrealized += pnl
        open_capital += sum(p * q for p, q, _ in pos.legs)
        if worst_open is None or pnl < worst_open[1]:
            worst_open = (sym, pnl)

    peak_date, peak_capital, peak_symbols = (
        max(result["capital_by_date"], key=lambda x: x[1]) if result["capital_by_date"] else (None, 0.0, [])
    )

    return {
        "closed_trades": len(trades),
        "win_rate": (len(wins) / len(trades)) if trades else 0.0,
        "realized": realized,
        "unrealized": unrealized,
        "combined": realized + unrealized,
        "still_open": len(still_open),
        "stuck": sum(1 for pos in still_open.values() if not pos.trailing),
        "open_capital": open_capital,
        "worst_open": worst_open,
        "avg_legs": (sum(t["legs"] for t in trades) / len(trades)) if trades else 0.0,
        "avg_hold_days": (sum((t["exit_date"] - t["entry_date"]).days for t in trades) / len(trades)) if trades else 0.0,
        "biggest_win": max((t["pnl"] for t in trades), default=0.0),
        "peak_capital": peak_capital,
        "peak_date": peak_date,
        "peak_symbols": peak_symbols,
    }
