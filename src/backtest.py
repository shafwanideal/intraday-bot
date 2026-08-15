import pandas as pd

from src.strategy import DAILY_LOSS_CAP, GRID_PCT, LEVERAGE, MARGIN_CAPITAL, SQUARE_OFF_TIME, GridEngine

# Re-exported for callers/tests that reach for these on this module.
__all__ = ["run_backtest", "summarize", "per_symbol_comparison", "GRID_PCT", "LEVERAGE", "MARGIN_CAPITAL", "DAILY_LOSS_CAP"]


def run_backtest(
    data: dict[str, pd.DataFrame],
    symbols: list[str] | None = None,
    directions: dict[str, str] | None = None,
    grid_pct: float = GRID_PCT,
    apply_costs: bool = True,
    leverage: float = LEVERAGE,
    daily_plan: dict | None = None,
    daily_loss_cap: float = DAILY_LOSS_CAP,
    trail_stop: bool = True,
    trail_pct: float | None = None,
) -> dict:
    """Simulate the grid strategy (see `src.strategy.GridEngine`) against real
    intraday bars, bar by bar.

    Entries only happen on the first bar of each day (market open), long by
    default unless `directions[symbol] == "short"`.

    If `daily_plan` is given (`{date: {symbol: direction}}`), it overrides
    `symbols`/`directions` entirely: only the dates present in the plan are
    simulated, and on each of those dates only that date's symbols are
    eligible to trade -- this mirrors the real workflow of a fresh, possibly
    different, stock list handed to the bot each morning.

    If `trail_stop` is True, hitting `grid_pct` favorable move doesn't close
    the position immediately -- instead it starts trailing a stop `trail_pct`
    behind the best price seen since (default: half of `grid_pct`, so at
    least half the original target is locked in even on an immediate reversal).
    """
    directions = directions or {}

    if daily_plan is not None:
        all_days = sorted(daily_plan.keys())
    else:
        symbols = symbols or []
        all_days = sorted({ts.date() for sym in symbols if sym in data for ts in data[sym].index})

    trade_log: list[dict] = []
    daily_results: list[dict] = []

    for day in all_days:
        day_symbols = list(daily_plan[day].keys()) if daily_plan is not None else symbols
        day_directions = daily_plan[day] if daily_plan is not None else directions

        day_bars = {
            sym: data[sym][data[sym].index.date == day]
            for sym in day_symbols
            if sym in data and not data[sym][data[sym].index.date == day].empty
        }
        if not day_bars:
            daily_results.append({"date": day, "pnl": 0.0, "halted_on_loss_cap": False, "note": "no data for this day's symbol(s)"})
            continue

        all_times = sorted({ts for df in day_bars.values() for ts in df.index})
        engine = GridEngine(
            grid_pct=grid_pct,
            apply_costs=apply_costs,
            leverage=leverage,
            daily_loss_cap=daily_loss_cap,
            trail_stop=trail_stop,
            trail_pct=trail_pct,
        )

        for i, t in enumerate(all_times):
            is_first_bar = i == 0
            is_square_off = t.time() >= SQUARE_OFF_TIME

            if is_first_bar:
                for symbol in day_symbols:
                    if symbol not in day_bars or t not in day_bars[symbol].index:
                        continue
                    price = day_bars[symbol].loc[t, "Open"]
                    direction = day_directions.get(symbol, "long")
                    engine.enter(symbol, price, direction, t)  # no-op if slots/capital exhausted

            if engine.halted:
                continue

            for symbol in list(engine.open_positions.keys()):
                if symbol not in day_bars or t not in day_bars[symbol].index:
                    continue
                engine.update(symbol, day_bars[symbol].loc[t, "Close"], t)

            current_prices = {
                sym: day_bars[sym].loc[t, "Close"] if sym in day_bars and t in day_bars[sym].index else pos.avg_price
                for sym, pos in engine.open_positions.items()
            }
            engine.check_loss_cap(current_prices, t)

            if is_square_off and engine.open_positions:
                current_prices = {
                    sym: day_bars[sym].loc[t, "Close"] if sym in day_bars and t in day_bars[sym].index else pos.avg_price
                    for sym, pos in engine.open_positions.items()
                }
                engine.square_off(current_prices, t)

        if engine.open_positions:
            # Safety net: no bar in this day's unified timeline reached SQUARE_OFF_TIME
            # (e.g. a symbol's feed ends a few minutes early) so the in-loop square-off
            # never fired. Force-close using each symbol's last known price rather than
            # silently leaving positions open and dropping their P&L.
            last_t = all_times[-1]
            current_prices = {
                sym: day_bars[sym].loc[last_t, "Close"] if sym in day_bars and last_t in day_bars[sym].index else pos.avg_price
                for sym, pos in engine.open_positions.items()
            }
            engine.square_off(current_prices, last_t)

        for entry in engine.trade_log:
            trade_log.append({"date": day, **entry})
        daily_results.append({"date": day, "pnl": engine.daily_pnl, "halted_on_loss_cap": engine.halted})

    return {"trade_log": trade_log, "daily_results": daily_results}


def summarize(results: dict, goal_daily_pnl: float | None = None) -> str:
    daily = pd.DataFrame(results["daily_results"])
    trades = pd.DataFrame(results["trade_log"])

    lines = []
    if daily.empty:
        return "No trading days simulated (no data)."

    total_pnl = daily["pnl"].sum()
    win_days = (daily["pnl"] > 0).sum()
    loss_days = (daily["pnl"] < 0).sum()
    cap_hit_days = daily["halted_on_loss_cap"].sum()

    lines.append(f"Days simulated: {len(daily)}")
    if not trades.empty and "costs" in trades:
        lines.append(f"Total gross P&L: Rs {trades['gross_pnl'].sum():,.2f}")
        lines.append(f"Total transaction costs: Rs {trades['costs'].sum():,.2f}")
    lines.append(f"Total net P&L: Rs {total_pnl:,.2f}")
    lines.append(f"Avg net P&L/day: Rs {daily['pnl'].mean():,.2f}")
    lines.append(f"Median net P&L/day: Rs {daily['pnl'].median():,.2f}")
    lines.append(f"Win days: {win_days}  Loss days: {loss_days}")
    lines.append(f"Days daily loss cap was hit: {cap_hit_days}")
    lines.append(f"Best day: Rs {daily['pnl'].max():,.2f}  Worst day: Rs {daily['pnl'].min():,.2f}")

    if goal_daily_pnl is not None:
        days_hit_goal = (daily["pnl"] >= goal_daily_pnl).sum()
        lines.append(
            f"Days net P&L >= Rs {goal_daily_pnl:,.0f} goal: {days_hit_goal}/{len(daily)} "
            f"({days_hit_goal / len(daily):.1%})"
        )

    if not trades.empty:
        lines.append(f"Total trades (legs closed): {len(trades)}")
        win_trades = (trades["pnl"] > 0).sum()
        lines.append(f"Win rate per closed position: {win_trades}/{len(trades)} ({win_trades / len(trades):.1%})")
        lines.append("Exit reason breakdown:")
        for reason, count in trades["reason"].value_counts().items():
            lines.append(f"  {reason}: {count}")

    return "\n".join(lines)


def per_symbol_comparison(
    data: dict[str, pd.DataFrame],
    symbols: list[str],
    directions: dict[str, str] | None = None,
    grid_pct: float = GRID_PCT,
    apply_costs: bool = True,
    leverage: float = LEVERAGE,
) -> pd.DataFrame:
    """Run each symbol through its own isolated backtest (full capital, no
    competition for the 3-slot/4-unit pool). Useful for comparing candidates
    on the morning stock list on equal footing, since running them together
    means only the first few in list order ever get a slot.
    """
    rows = []
    for symbol in symbols:
        if symbol not in data:
            continue
        result = run_backtest(
            {symbol: data[symbol]},
            symbols=[symbol],
            directions=directions,
            grid_pct=grid_pct,
            apply_costs=apply_costs,
            leverage=leverage,
        )
        daily = pd.DataFrame(result["daily_results"])
        trades = pd.DataFrame(result["trade_log"])
        if daily.empty:
            continue
        rows.append(
            {
                "symbol": symbol,
                "days": len(daily),
                "gross_pnl": round(trades["gross_pnl"].sum(), 2) if not trades.empty else 0,
                "costs": round(trades["costs"].sum(), 2) if not trades.empty else 0,
                "net_pnl": round(daily["pnl"].sum(), 2),
                "avg_net_pnl_day": round(daily["pnl"].mean(), 2),
                "win_days": int((daily["pnl"] > 0).sum()),
                "loss_days": int((daily["pnl"] < 0).sum()),
                "trades": len(trades),
                "win_rate": round((trades["pnl"] > 0).mean(), 3) if len(trades) else None,
                "loss_cap_hits": int(daily["halted_on_loss_cap"].sum()),
            }
        )
    return pd.DataFrame(rows)
