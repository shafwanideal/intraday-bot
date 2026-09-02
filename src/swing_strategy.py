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

    @property
    def qty(self) -> int:
        return sum(q for _, q, _ in self.legs)

    @property
    def avg_price(self) -> float:
        return sum(p * q for p, q, _ in self.legs) / self.qty

    @property
    def last_leg_price(self) -> float:
        return self.legs[-1][0]


class SwingEngine:
    """One independent capital pool per stock (Rs 2,00,000-per-leg by
    default) -- NOT a shared pool across concurrent positions like the
    intraday GridEngine's unit system, since this was specified per-stock
    ("you deploy 2 lakhs each"). No square-off, no daily loss cap, no
    stop-loss -- positions close only on hitting PROFIT_TARGET_PCT, or stay
    open indefinitely (reported as still-open/unrealized at the end of a
    backtest).
    """

    def __init__(
        self,
        capital_per_leg: float = CAPITAL_PER_LEG,
        averaging_drop_pct: float = AVERAGING_DROP_PCT,
        profit_target_pct: float = PROFIT_TARGET_PCT,
        apply_costs: bool = True,
    ):
        self.capital_per_leg = capital_per_leg
        self.averaging_drop_pct = averaging_drop_pct
        self.profit_target_pct = profit_target_pct
        self.apply_costs = apply_costs
        self.open_positions: dict[str, SwingPosition] = {}
        self.closed_trades: list[dict] = []

    def can_enter(self, symbol: str) -> bool:
        return symbol not in self.open_positions

    def enter(self, symbol: str, price: float, date) -> bool:
        if not self.can_enter(symbol):
            return False
        qty = math.floor(self.capital_per_leg / price)
        if qty < 1:
            return False
        self.open_positions[symbol] = SwingPosition(symbol=symbol, legs=[(price, qty, date)], entry_qty=qty)
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

    def update(self, symbol: str, price: float, date) -> dict | None:
        """Call once per trading day (using that day's close) for each open
        position. Checks the profit target first (using the CURRENT blended
        average, recomputed after every leg), then averaging."""
        pos = self.open_positions.get(symbol)
        if pos is None:
            return None
        if price >= pos.avg_price * (1 + self.profit_target_pct):
            return self._close(symbol, price, date, "profit_target")
        if price <= pos.last_leg_price * (1 - self.averaging_drop_pct):
            pos.legs.append((price, pos.entry_qty, date))
        return None
