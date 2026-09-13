import pandas as pd

from src.strategy import (
    DAILY_LOSS_CAP,
    DEFAULT_ATR_MULTIPLIER,
    GRID_PCT,
    LEVERAGE,
    MARGIN_CAPITAL,
    MAX_CONCURRENT_POSITIONS,
    SQUARE_OFF_TIME,
    TOTAL_UNITS,
    GridEngine,
)

# Re-exported for callers/tests that reach for these on this module.
__all__ = [
    "run_backtest",
    "summarize",
    "per_symbol_comparison",
    "parse_plan_entry",
    "validate_day_plan_allocations",
    "GRID_PCT",
    "LEVERAGE",
    "MARGIN_CAPITAL",
    "DAILY_LOSS_CAP",
    "DEFAULT_ATR_MULTIPLIER",
]


def parse_plan_entry(entry: str | dict) -> tuple[str, float | None]:
    """A daily_plan entry for one symbol is either a plain direction string
    (`"long"`) -- equal capital split across the day's slots, the original
    behavior -- or `{"direction": "long", "pct": 33}` to size that position
    at an explicit percentage of margin_capital instead. Returns
    (direction, pct_or_None). Shared by run_backtest and the scripts that
    build a daily_plan by hand, so both apply the identical rule."""
    if isinstance(entry, str):
        return entry, None
    direction = entry.get("direction")
    pct = entry.get("pct")
    if direction not in ("long", "short"):
        raise ValueError(f"Invalid direction {direction!r} in plan entry {entry!r} -- must be 'long' or 'short'.")
    if pct is not None:
        pct = float(pct)
        if not (0 < pct <= 100):
            raise ValueError(f"pct must be between 0 and 100, got {pct} in plan entry {entry!r}.")
    return direction, pct


def validate_day_plan_allocations(day_plan: dict[str, str | dict]) -> None:
    """A day's plan must be ALL-or-NOTHING on explicit percentages: either
    every symbol gives one (so the capital math is unambiguous) or none do
    (falling back to the original equal-split behavior). Mixing the two
    would leave the un-allocated symbols' share undefined -- rather than
    silently falling back to some improvised leftover split, this is a
    deliberate configuration mistake worth stopping on immediately.
    Also rejects percentages that sum past 100 -- that's asking to deploy
    more than the day's margin capital, almost always a typo."""
    parsed = {sym: parse_plan_entry(entry) for sym, entry in day_plan.items()}
    has_pct = {sym: pct is not None for sym, (_, pct) in parsed.items()}
    if any(has_pct.values()) and not all(has_pct.values()):
        missing = sorted(sym for sym, given in has_pct.items() if not given)
        raise ValueError(
            f"Some symbols have an explicit pct allocation and some don't: {missing} are missing one. "
            "Give every symbol a pct, or none at all (equal split)."
        )
    total_pct = sum(pct for _, pct in parsed.values() if pct is not None)
    if total_pct > 100 + 1e-9:
        raise ValueError(f"Percentages sum to {total_pct:.2f}%, which is over 100% of margin capital.")


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
    symbol_atr: dict[str, float] | None = None,
    atr_multiplier: float | None = None,
    trail_grace_minutes: float = 0,
    per_stock_stop_loss: float | None = None,
    portfolio_profit_lock_trigger: float | None = None,
    portfolio_profit_lock_giveback: float | None = None,
    enable_averaging: bool = True,
    trailing_activation_pct: float | None = None,
    averaging_pct: float | None = None,
    profit_exit: bool = True,
    margin_capital: float = MARGIN_CAPITAL,
    total_units: int = TOTAL_UNITS,
    max_concurrent_positions: int = MAX_CONCURRENT_POSITIONS,
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
    the position immediately -- instead it starts trailing a stop behind the
    best price seen since. By default that trail distance is `trail_pct`
    (half of `grid_pct` if not set) as a percentage of price. If
    `atr_multiplier` is given, the trail distance becomes
    `atr_multiplier * symbol_atr[symbol]` (absolute price units) instead --
    a volatility-scaled trail rather than one fixed percentage for every
    stock. `symbol_atr` should be precomputed (e.g. via
    `src.indicators.atr` on `src.kite_data.fetch_daily` bars) and is only
    used for symbols present in it; symbols missing from it fall back to
    the percentage-based trail even when `atr_multiplier` is set.
    """
    directions = directions or {}
    symbol_atr = symbol_atr or {}

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
        if daily_plan is not None:
            validate_day_plan_allocations(daily_plan[day])

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
            atr_multiplier=atr_multiplier,
            trail_grace_minutes=trail_grace_minutes,
            per_stock_stop_loss=per_stock_stop_loss,
            portfolio_profit_lock_trigger=portfolio_profit_lock_trigger,
            portfolio_profit_lock_giveback=portfolio_profit_lock_giveback,
            enable_averaging=enable_averaging,
            trailing_activation_pct=trailing_activation_pct,
            averaging_pct=averaging_pct,
            profit_exit=profit_exit,
            margin_capital=margin_capital,
            total_units=total_units,
            max_concurrent_positions=max_concurrent_positions,
        )

        last_known_price: dict[str, float] = {}

        for i, t in enumerate(all_times):
            is_first_bar = i == 0
            is_square_off = t.time() >= SQUARE_OFF_TIME

            if is_first_bar:
                for symbol in day_symbols:
                    if symbol not in day_bars or t not in day_bars[symbol].index:
                        continue
                    price = day_bars[symbol].loc[t, "Open"]
                    direction, pct = parse_plan_entry(day_directions.get(symbol, "long"))
                    # pct given -> size this position at exactly pct% of margin_capital
                    # (leveraged), overriding the engine's own equal-split exposure_per_unit.
                    # None -> unchanged original behavior (equal split across total_units).
                    quantity = (pct / 100.0 * margin_capital * leverage) / price if pct is not None else None
                    if engine.enter(symbol, price, direction, t, atr=symbol_atr.get(symbol), quantity=quantity):
                        last_known_price[symbol] = price

            if engine.halted:
                continue

            for symbol in list(engine.open_positions.keys()):
                if symbol not in day_bars or t not in day_bars[symbol].index:
                    continue
                price = day_bars[symbol].loc[t, "Close"]
                last_known_price[symbol] = price
                engine.update(symbol, price, t)

            # Fall back to each symbol's own last known price (not avg_price/entry
            # price) when this timestamp isn't in that symbol's bar index -- e.g. one
            # symbol's feed ends earlier in the day than another's. Falling back to
            # avg_price would fabricate a "zero movement" close that's just wrong.
            current_prices = {
                sym: last_known_price.get(sym, pos.avg_price) for sym, pos in engine.open_positions.items()
            }
            engine.check_loss_cap(current_prices, t)
            if engine.open_positions:
                current_prices = {
                    sym: last_known_price.get(sym, pos.avg_price) for sym, pos in engine.open_positions.items()
                }
                engine.check_per_stock_stop_loss(current_prices, t)
            if engine.open_positions:
                current_prices = {
                    sym: last_known_price.get(sym, pos.avg_price) for sym, pos in engine.open_positions.items()
                }
                engine.check_portfolio_profit_lock(current_prices, t)

            if is_square_off and engine.open_positions:
                current_prices = {
                    sym: last_known_price.get(sym, pos.avg_price) for sym, pos in engine.open_positions.items()
                }
                engine.square_off(current_prices, t)

        if engine.open_positions:
            # Safety net: no bar in this day's unified timeline reached SQUARE_OFF_TIME
            # (e.g. a symbol's feed ends a few minutes early) so the in-loop square-off
            # never fired. Force-close using each symbol's last known price rather than
            # silently leaving positions open and dropping their P&L.
            last_t = all_times[-1]
            current_prices = {
                sym: last_known_price.get(sym, pos.avg_price) for sym, pos in engine.open_positions.items()
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
    daily_loss_cap: float = DAILY_LOSS_CAP,
    trail_stop: bool = True,
    trail_pct: float | None = None,
    symbol_atr: dict[str, float] | None = None,
    atr_multiplier: float | None = None,
) -> pd.DataFrame:
    """Run each symbol through its own isolated backtest (full capital, no
    competition for the 3-slot/4-unit pool). Useful for comparing candidates
    on the morning stock list on equal footing, since running them together
    means only the first few in list order ever get a slot.
    """
    symbol_atr = symbol_atr or {}
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
            daily_loss_cap=daily_loss_cap,
            trail_stop=trail_stop,
            trail_pct=trail_pct,
            symbol_atr={symbol: symbol_atr[symbol]} if symbol in symbol_atr else None,
            atr_multiplier=atr_multiplier,
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
