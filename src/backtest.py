from dataclasses import dataclass, field
from datetime import time

import pandas as pd

MARGIN_CAPITAL = 50_000  # real cash at risk; the daily loss cap and 4-unit slots are against this
TOTAL_UNITS = 4
MARGIN_PER_UNIT = MARGIN_CAPITAL / TOTAL_UNITS  # 12,500
LEVERAGE = 5  # Zerodha MIS intraday leverage on equity; varies per stock in reality
MAX_CONCURRENT_POSITIONS = 3
GRID_PCT = 0.02
DAILY_LOSS_CAP = 5_000
SQUARE_OFF_TIME = time(15, 15)

# Zerodha intraday equity (non-delivery) charges, applied per order.
BROKERAGE_RATE = 0.0003  # 0.03%, capped at Rs 20/order
BROKERAGE_CAP = 20.0
STT_RATE = 0.00025  # 0.025%, sell side only
EXCHANGE_TXN_RATE = 0.0000297  # NSE, both sides
SEBI_RATE = 0.000001  # both sides
STAMP_DUTY_RATE = 0.00003  # 0.003%, buy side only
GST_RATE = 0.18  # on brokerage + exchange txn charges


def order_cost(value: float, side: str) -> float:
    """Real round-number Zerodha intraday equity charges for one order leg."""
    brokerage = min(BROKERAGE_CAP, BROKERAGE_RATE * value)
    exchange_txn = EXCHANGE_TXN_RATE * value
    sebi = SEBI_RATE * value
    gst = GST_RATE * (brokerage + exchange_txn)
    stt = STT_RATE * value if side == "sell" else 0.0
    stamp = STAMP_DUTY_RATE * value if side == "buy" else 0.0
    return brokerage + exchange_txn + sebi + gst + stt + stamp


def position_cost(direction: str, legs: list[tuple[float, float]], exit_price: float, exit_qty: float) -> float:
    entry_side = "buy" if direction == "long" else "sell"
    exit_side = "sell" if direction == "long" else "buy"
    total = sum(order_cost(price * qty, entry_side) for price, qty in legs)
    total += order_cost(exit_price * exit_qty, exit_side)
    return total


@dataclass
class Position:
    symbol: str
    direction: str  # "long" or "short"
    entry_time: pd.Timestamp
    legs: list = field(default_factory=list)  # list of (price, qty)
    averaged: bool = False

    @property
    def qty(self) -> float:
        return sum(q for _, q in self.legs)

    @property
    def avg_price(self) -> float:
        return sum(p * q for p, q in self.legs) / self.qty

    @property
    def units_used(self) -> int:
        return len(self.legs)


def _pnl(direction: str, avg_price: float, exit_price: float, qty: float) -> float:
    if direction == "long":
        return (exit_price - avg_price) * qty
    return (avg_price - exit_price) * qty


def _move_pct(direction: str, avg_price: float, price: float) -> float:
    if direction == "long":
        return (price - avg_price) / avg_price
    return (avg_price - price) / avg_price


def run_backtest(
    data: dict[str, pd.DataFrame],
    symbols: list[str] | None = None,
    directions: dict[str, str] | None = None,
    grid_pct: float = GRID_PCT,
    apply_costs: bool = True,
    leverage: float = LEVERAGE,
    daily_plan: dict | None = None,
) -> dict:
    """Simulate the grid strategy against real intraday bars.

    Rules implemented (see project brief): 1 unit (of 4, ~Rs 12,500 margin
    each) at entry; one averaging leg on a `grid_pct` adverse move; full
    exit on a `grid_pct` favorable move from average cost; max 3 concurrent
    positions sharing the 4-unit margin pool; daily loss cap of Rs 5,000 is
    checked mark-to-market (realized + unrealized P&L on open positions)
    every bar, and halts/flattens everything for the day the moment it's
    breached; hard square-off of anything still open at 15:15.

    Each unit's actual traded value is `MARGIN_PER_UNIT * leverage` (Zerodha
    MIS intraday leverage), not the raw margin -- the 4-slot bookkeeping
    tracks margin blocks, but position sizing and P&L/costs are computed on
    the leveraged notional actually bought/sold.

    Entries only happen on the first bar of each day (market open), long by
    default unless `directions[symbol] == "short"`. When `apply_costs` is
    True, real Zerodha intraday brokerage/STT/exchange/stamp/GST charges are
    deducted from each closed position's P&L.

    If `daily_plan` is given (`{date: {symbol: direction}}`), it overrides
    `symbols`/`directions` entirely: only the dates present in the plan are
    simulated, and on each of those dates only that date's symbols are
    eligible to trade -- this mirrors the real workflow of a fresh, possibly
    different, stock list handed to the bot each morning.
    """
    directions = directions or {}
    exposure_per_unit = MARGIN_PER_UNIT * leverage

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

        capital_units_available = TOTAL_UNITS
        open_positions: dict[str, Position] = {}
        daily_pnl = 0.0
        halted = False

        def close(symbol: str, pos: Position, exit_price: float, reason: str, t: pd.Timestamp) -> None:
            nonlocal daily_pnl, capital_units_available
            gross_pnl = _pnl(pos.direction, pos.avg_price, exit_price, pos.qty)
            costs = position_cost(pos.direction, pos.legs, exit_price, pos.qty) if apply_costs else 0.0
            pnl = gross_pnl - costs
            daily_pnl += pnl
            capital_units_available += pos.units_used
            trade_log.append(
                {
                    "date": day,
                    "symbol": symbol,
                    "direction": pos.direction,
                    "entry_time": pos.entry_time,
                    "exit_time": t,
                    "avg_price": pos.avg_price,
                    "exit_price": exit_price,
                    "qty": pos.qty,
                    "units_used": pos.units_used,
                    "gross_pnl": gross_pnl,
                    "costs": costs,
                    "pnl": pnl,
                    "reason": reason,
                }
            )

        for i, t in enumerate(all_times):
            is_first_bar = i == 0
            is_square_off = t.time() >= SQUARE_OFF_TIME

            if is_first_bar and not halted:
                for symbol in day_symbols:
                    if symbol not in day_bars or t not in day_bars[symbol].index:
                        continue
                    if symbol in open_positions:
                        continue
                    if len(open_positions) >= MAX_CONCURRENT_POSITIONS or capital_units_available < 1:
                        break
                    price = day_bars[symbol].loc[t, "Open"]
                    direction = day_directions.get(symbol, "long")
                    qty = exposure_per_unit / price
                    open_positions[symbol] = Position(
                        symbol=symbol, direction=direction, entry_time=t, legs=[(price, qty)]
                    )
                    capital_units_available -= 1

            if halted:
                continue

            for symbol in list(open_positions.keys()):
                if symbol not in day_bars or t not in day_bars[symbol].index:
                    continue
                pos = open_positions[symbol]
                price = day_bars[symbol].loc[t, "Close"]
                move = _move_pct(pos.direction, pos.avg_price, price)

                if move >= grid_pct:
                    close(symbol, pos, price, "target_exit", t)
                    del open_positions[symbol]
                    continue

                if not pos.averaged and move <= -grid_pct and capital_units_available >= 1:
                    qty = exposure_per_unit / price
                    pos.legs.append((price, qty))
                    pos.averaged = True
                    capital_units_available -= 1

            if not halted:
                unrealized_pnl = 0.0
                for symbol, pos in open_positions.items():
                    price = day_bars[symbol].loc[t, "Close"] if t in day_bars[symbol].index else pos.avg_price
                    unrealized_pnl += _pnl(pos.direction, pos.avg_price, price, pos.qty)

                if daily_pnl + unrealized_pnl <= -DAILY_LOSS_CAP:
                    halted = True
                    for symbol in list(open_positions.keys()):
                        pos = open_positions[symbol]
                        price = day_bars[symbol].loc[t, "Close"] if t in day_bars[symbol].index else pos.avg_price
                        close(symbol, pos, price, "daily_loss_cap", t)
                    open_positions.clear()

            if is_square_off and open_positions:
                for symbol in list(open_positions.keys()):
                    pos = open_positions[symbol]
                    price = day_bars[symbol].loc[t, "Close"] if t in day_bars[symbol].index else pos.avg_price
                    close(symbol, pos, price, "square_off", t)
                open_positions.clear()

        daily_results.append({"date": day, "pnl": daily_pnl, "halted_on_loss_cap": halted})

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
