"""Swing/positional variant of the grid-averaging idea, requested 2026-09-02:
unlimited-leg averaging (fixed share quantity per leg, not fixed rupee
value) every AVERAGING_DROP_PCT fall from the last fill, exits the WHOLE
position at PROFIT_TARGET_PCT above the current blended average, holds
across multiple days (no intraday square-off -- this is CNC/delivery, not
MIS). Deliberately has NO stop-loss and NO cap on the number of averaging
legs, per explicit instruction ("wait if it falls do the same again until
there is a profit of 2%") -- a real, open-ended capital-commitment risk on
a stock that keeps falling, not an oversight.

Separate from src/strategy.py's GridEngine (single intraday day, one
averaging leg, MIS costs) rather than bolted onto it -- the two share the
"average into a dip" idea but differ in almost every mechanical way
(holding period, leg count, cost structure, exit rule).
"""

import math
from dataclasses import dataclass, field

CAPITAL_PER_LEG = 200_000  # Rs 2,00,000 -- deployed on the FIRST leg; every subsequent
# averaging leg buys the SAME SHARE QUANTITY as that first leg (not the same rupee
# value) -- as prices fall, each successive leg costs less in rupees than the last.
AVERAGING_DROP_PCT = 0.03  # add another leg once price falls this much from the LAST fill
PROFIT_TARGET_PCT = 0.02  # close the WHOLE position once price is this much above the
# CURRENT blended average -- recomputed after every leg, not fixed to the first entry.

# Real Zerodha equity DELIVERY (CNC) charges -- materially different from the intraday
# (MIS) charges in strategy.py: zero brokerage, but STT is charged on BOTH sides (not
# just sell) and at 4x the intraday rate, and stamp duty is 5x the intraday rate.
DELIVERY_BROKERAGE = 0.0  # Zerodha charges zero brokerage on equity delivery
DELIVERY_STT_RATE = 0.001  # 0.1%, both buy and sell (intraday MIS is 0.025%, sell-only)
DELIVERY_EXCHANGE_TXN_RATE = 0.0000297  # NSE, both sides -- same rate as intraday
DELIVERY_SEBI_RATE = 0.000001  # both sides -- same as intraday
DELIVERY_STAMP_DUTY_RATE = 0.00015  # 0.015% buy side only (intraday MIS is 0.003%)
DELIVERY_GST_RATE = 0.18  # on brokerage + exchange txn charges (brokerage is 0, so this
# only actually taxes the exchange txn charge here)
DELIVERY_DP_CHARGE = 15.93  # Zerodha's flat DP (depository participant) charge per
# scrip per sell day, CDSL fee + GST included -- approximate, varies slightly; only
# applies once per SELL of a given scrip on a given day, not per buy or per leg.


def delivery_order_cost(value: float, side: str) -> float:
    """Real Zerodha equity delivery charges for one order leg. `side` is "buy" or "sell"."""
    brokerage = DELIVERY_BROKERAGE
    exchange_txn = DELIVERY_EXCHANGE_TXN_RATE * value
    sebi = DELIVERY_SEBI_RATE * value
    gst = DELIVERY_GST_RATE * (brokerage + exchange_txn)
    stt = DELIVERY_STT_RATE * value  # both sides for delivery
    stamp = DELIVERY_STAMP_DUTY_RATE * value if side == "buy" else 0.0
    dp = DELIVERY_DP_CHARGE if side == "sell" else 0.0
    return brokerage + exchange_txn + sebi + gst + stt + stamp + dp


@dataclass
class SwingPosition:
    symbol: str
    legs: list = field(default_factory=list)  # [(price, qty, date)]
    entry_qty: int = 0  # fixed share count reused for every averaging leg
    trailing: bool = False  # armed once price first hits PROFIT_TARGET_PCT above avg_price
    peak_price: float | None = None  # best close seen since trailing armed
    atr: float | None = None  # this symbol's ATR at entry, if using ATR-based trailing

    @property
    def qty(self) -> int:
        return sum(q for _, q, _ in self.legs)

    @property
    def avg_price(self) -> float:
        return sum(p * q for p, q, _ in self.legs) / self.qty

    @property
    def last_leg_price(self) -> float:
        return self.legs[-1][0]


TRAILING_PCT = 0.01  # requested 2026-09-02: once armed at PROFIT_TARGET_PCT, trail the peak
# instead of closing immediately. No distance was specified, so this defaults to half the
# 2% activation threshold -- same convention already used by the intraday strategy
# (trail_pct defaults to grid_pct / 2 there too).


class SwingEngine:
    """One independent capital pool per stock (Rs 2,00,000-per-leg by
    default) -- NOT a shared pool across concurrent positions like the
    intraday GridEngine's unit system, since this was specified per-stock
    ("you deploy 2 lakhs each"). No square-off, no daily loss cap, no
    stop-loss -- once PROFIT_TARGET_PCT arms trailing, a position closes
    only when it pulls back TRAILING_PCT from its peak since arming, or
    stays open indefinitely (reported as still-open/unrealized at the end
    of a backtest).
    """

    def __init__(
        self,
        capital_per_leg: float = CAPITAL_PER_LEG,
        averaging_drop_pct: float = AVERAGING_DROP_PCT,
        profit_target_pct: float = PROFIT_TARGET_PCT,
        trailing_pct: float = TRAILING_PCT,
        atr_multiplier: float | None = None,
        apply_costs: bool = True,
        max_total_capital: float | None = None,
        stop_fill_at_level: bool = False,
    ):
        self.capital_per_leg = capital_per_leg
        self.averaging_drop_pct = averaging_drop_pct
        self.profit_target_pct = profit_target_pct
        self.trailing_pct = trailing_pct
        # If set, trail distance = atr_multiplier * the symbol's ATR (absolute price
        # units, passed to enter()) instead of trailing_pct -- same convention as the
        # intraday GridEngine's atr_multiplier. Symbols entered without an atr value
        # fall back to the percentage-based trail even when this is set.
        self.atr_multiplier = atr_multiplier
        self.apply_costs = apply_costs
        # Hard ceiling on TOTAL capital committed across every open position's every leg,
        # combined -- added 2026-09-02 after backtests showed peak concurrent capital
        # requirements of 10-100x a real Rs 15,00,000 budget with no cap at all. None means
        # uncapped (old behavior). Both a brand-new entry AND an averaging leg on an
        # existing position are refused if they'd push total committed capital past this.
        self.max_total_capital = max_total_capital
        # A trailing stop is a RESTING ORDER: it fills when price trades through the
        # stop level, not at whatever that day happened to close at. With this False
        # (the historical behavior) an exit is booked at the close, so a day that
        # breaches the stop early and keeps falling books the full day's decline as if
        # the stop never existed -- on GODFRYPHLP 2026-02-23 that turned a ~2485 stop
        # into a 2213 fill, roughly Rs 1,11,000 of phantom loss on one trade, enough to
        # flip the sign of a whole backtest. With it True the fill is the stop level,
        # or the day's open when the stock opened below the stop (gap-down: the resting
        # order fills at the open, and no stop protects against a gap).
        # Still optimistic: it ignores slippage past the level in a fast market.
        self.stop_fill_at_level = stop_fill_at_level
        self.open_positions: dict[str, SwingPosition] = {}
        self.closed_trades: list[dict] = []

    def total_capital_committed(self) -> float:
        return sum(sum(p * q for p, q, _ in pos.legs) for pos in self.open_positions.values())

    def _capital_available_for(self, cost: float) -> bool:
        if self.max_total_capital is None:
            return True
        return self.total_capital_committed() + cost <= self.max_total_capital

    def can_enter(self, symbol: str) -> bool:
        if symbol in self.open_positions:
            return False
        return self._capital_available_for(self.capital_per_leg)

    def enter(self, symbol: str, price: float, date, atr: float | None = None) -> bool:
        if not self.can_enter(symbol):
            return False
        qty = math.floor(self.capital_per_leg / price)
        if qty < 1:
            return False
        self.open_positions[symbol] = SwingPosition(symbol=symbol, legs=[(price, qty, date)], entry_qty=qty, atr=atr)
        return True

    def _close(self, symbol: str, price: float, date, reason: str) -> dict:
        pos = self.open_positions.pop(symbol)
        gross_pnl = (price - pos.avg_price) * pos.qty
        if self.apply_costs:
            costs = sum(delivery_order_cost(p * q, "buy") for p, q, _ in pos.legs)
            costs += delivery_order_cost(price * pos.qty, "sell")
        else:
            costs = 0.0
        net_pnl = gross_pnl - costs
        entry = {
            "symbol": symbol,
            "entry_date": pos.legs[0][2],
            "exit_date": date,
            "avg_price": pos.avg_price,
            "exit_price": price,
            "qty": pos.qty,
            "legs": len(pos.legs),
            "capital_deployed": sum(p * q for p, q, _ in pos.legs),
            "gross_pnl": gross_pnl,
            "costs": costs,
            "pnl": net_pnl,
            "reason": reason,
        }
        self.closed_trades.append(entry)
        return entry

    def update(self, symbol: str, price: float, date, low: float | None = None, open_: float | None = None) -> dict | None:
        """Call once per trading day (using that day's close) for each open
        position. `low` and `open_` are that day's low and open; they are only
        used when `stop_fill_at_level` is set, to fill a breached trailing stop
        at the stop level rather than at the close.

        Once armed (price first reaches profit_target_pct above the CURRENT
        blended average -- recomputed after every leg), the position no
        longer closes immediately -- it trails: peak_price tracks the best
        close since arming, and the position closes if price pulls back
        trailing_pct from that peak. Averaging stops being checked once
        trailing is armed, same as the intraday engine's behavior -- once a
        position is genuinely in profit, the only decision left is when to
        take it, not whether to add more risk to it.
        """
        pos = self.open_positions.get(symbol)
        if pos is None:
            return None

        if pos.trailing:
            pos.peak_price = max(pos.peak_price, price)
            use_atr = self.atr_multiplier is not None and pos.atr is not None
            stop_level = (
                pos.peak_price - self.atr_multiplier * pos.atr if use_atr else pos.peak_price * (1 - self.trailing_pct)
            )
            if self.stop_fill_at_level and low is not None and low <= stop_level:
                # Gap-down below the stop: a resting order fills at the open, not at a
                # level the stock never traded at after the bell.
                fill = min(open_, stop_level) if open_ is not None else stop_level
                return self._close(symbol, fill, date, "trailing_stop_exit")
            if price <= stop_level:
                return self._close(symbol, price, date, "trailing_stop_exit")
            return None

        if price >= pos.avg_price * (1 + self.profit_target_pct):
            pos.trailing = True
            pos.peak_price = price
            return None

        if price <= pos.last_leg_price * (1 - self.averaging_drop_pct):
            leg_cost = price * pos.entry_qty
            if self._capital_available_for(leg_cost):
                pos.legs.append((price, pos.entry_qty, date))
            # else: no capital available -- skip this averaging leg, position stays as-is
            # and gets re-checked on later days (capital may free up as other positions close).
        return None
