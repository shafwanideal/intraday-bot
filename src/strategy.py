from dataclasses import dataclass, field
from datetime import time, timedelta

MARGIN_CAPITAL = 50_000  # real cash at risk; the daily loss cap and 4-unit slots are against this
TOTAL_UNITS = 4
MARGIN_PER_UNIT = MARGIN_CAPITAL / TOTAL_UNITS  # 12,500
LEVERAGE = 5  # Zerodha MIS intraday leverage on equity; varies per stock in reality
MAX_CONCURRENT_POSITIONS = 3
GRID_PCT = 0.02
DAILY_LOSS_CAP = 10_000  # raised from Rs 5,000 -- see grid_pct_and_costs memory for the tradeoff
SQUARE_OFF_TIME = time(15, 15)
DEFAULT_ATR_MULTIPLIER = 1.0  # trail distance = this * symbol's 14-day ATR
# Raised from 0.5 (2026-08-15) after user's actual picks (catalyst/earnings-driven,
# often gap-and-go at open) showed 1.0x beating 0.5x by 24% on 34 real trade cases,
# even though 0.5x is still better on calmer generic large caps -- see
# grid_pct_and_costs memory for the full comparison and reasoning.

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
    entry_time: object
    legs: list = field(default_factory=list)  # list of (price, qty)
    averaged: bool = False
    trailing: bool = False
    peak_price: float | None = None  # best favorable price seen once trailing has started
    atr: float | None = None  # this symbol's ATR at entry, if using volatility-based trailing
    trailing_activated_at: object = None  # timestamp trailing started, for the grace period

    @property
    def qty(self) -> float:
        return sum(q for _, q in self.legs)

    @property
    def avg_price(self) -> float:
        return sum(p * q for p, q in self.legs) / self.qty

    @property
    def units_used(self) -> int:
        return len(self.legs)


def pnl(direction: str, avg_price: float, exit_price: float, qty: float) -> float:
    if direction == "long":
        return (exit_price - avg_price) * qty
    return (avg_price - exit_price) * qty


def move_pct(direction: str, avg_price: float, price: float) -> float:
    if direction == "long":
        return (price - avg_price) / avg_price
    return (avg_price - price) / avg_price


class GridEngine:
    """Single-day grid-strategy state machine: 1 unit at entry, one averaging
    leg on a `grid_pct` adverse move, full exit on a `grid_pct` favorable
    move from average cost, max `MAX_CONCURRENT_POSITIONS` positions sharing
    a `TOTAL_UNITS`-unit margin pool, mark-to-market daily loss cap.

    Driven bar-by-bar by the backtest engine or poll-by-poll by the shadow
    (live paper-trading) engine -- both use this exact same logic so their
    behavior can't drift apart.
    """

    def __init__(
        self,
        grid_pct: float = GRID_PCT,
        apply_costs: bool = True,
        leverage: float = LEVERAGE,
        daily_loss_cap: float = DAILY_LOSS_CAP,
        trail_stop: bool = True,
        trail_pct: float | None = None,
        atr_multiplier: float | None = None,
        trail_grace_minutes: float = 0,
    ):
        self.grid_pct = grid_pct
        self.apply_costs = apply_costs
        self.exposure_per_unit = MARGIN_PER_UNIT * leverage
        self.daily_loss_cap = daily_loss_cap
        self.trail_stop = trail_stop
        self.trail_pct = trail_pct if trail_pct is not None else grid_pct / 2
        self.atr_multiplier = atr_multiplier  # if set, trail distance = atr_multiplier * position's ATR (price units) instead of trail_pct
        self.trail_grace_minutes = trail_grace_minutes  # no trailing-stop exit allowed this long after trailing activates
        self.capital_units_available = TOTAL_UNITS
        self.open_positions: dict[str, Position] = {}
        self.daily_pnl = 0.0
        self.halted = False
        self.trade_log: list[dict] = []

    def can_enter(self, symbol: str) -> bool:
        return (
            not self.halted
            and symbol not in self.open_positions
            and len(self.open_positions) < MAX_CONCURRENT_POSITIONS
            and self.capital_units_available >= 1
        )

    def enter(
        self, symbol: str, price: float, direction: str, timestamp, atr: float | None = None, quantity: float | None = None
    ) -> bool:
        """`quantity` overrides the computed `exposure_per_unit / price` size --
        callers placing real orders should pass the REAL filled quantity here
        (whole shares) so the engine's bookkeeping matches what's actually
        held, rather than the fractional paper-trading default."""
        if not self.can_enter(symbol):
            return False
        qty = quantity if quantity is not None else self.exposure_per_unit / price
        self.open_positions[symbol] = Position(
            symbol=symbol, direction=direction, entry_time=timestamp, legs=[(price, qty)], atr=atr
        )
        self.capital_units_available -= 1
        return True

    def _close(self, symbol: str, price: float, reason: str, timestamp) -> dict:
        pos = self.open_positions.pop(symbol)
        gross_pnl = pnl(pos.direction, pos.avg_price, price, pos.qty)
        costs = position_cost(pos.direction, pos.legs, price, pos.qty) if self.apply_costs else 0.0
        net_pnl = gross_pnl - costs
        self.daily_pnl += net_pnl
        self.capital_units_available += pos.units_used
        entry = {
            "symbol": symbol,
            "direction": pos.direction,
            "entry_time": pos.entry_time,
            "exit_time": timestamp,
            "avg_price": pos.avg_price,
            "exit_price": price,
            "qty": pos.qty,
            "units_used": pos.units_used,
            "gross_pnl": gross_pnl,
            "costs": costs,
            "pnl": net_pnl,
            "reason": reason,
        }
        self.trade_log.append(entry)
        return entry

    def update(self, symbol: str, price: float, timestamp) -> dict | None:
        """Check target-exit / trailing-stop / averaging for one open position
        at the current price."""
        if self.halted or symbol not in self.open_positions:
            return None
        pos = self.open_positions[symbol]

        if pos.trailing:
            use_atr = self.atr_multiplier is not None and pos.atr is not None
            if pos.direction == "long":
                pos.peak_price = max(pos.peak_price, price)
                if use_atr:
                    triggered = (pos.peak_price - price) >= self.atr_multiplier * pos.atr
                else:
                    triggered = (pos.peak_price - price) / pos.peak_price >= self.trail_pct
            else:
                pos.peak_price = min(pos.peak_price, price)
                if use_atr:
                    triggered = (price - pos.peak_price) >= self.atr_multiplier * pos.atr
                else:
                    triggered = (price - pos.peak_price) / pos.peak_price >= self.trail_pct
            in_grace = (
                self.trail_grace_minutes > 0
                and pos.trailing_activated_at is not None
                and (timestamp - pos.trailing_activated_at) < timedelta(minutes=self.trail_grace_minutes)
            )
            if triggered and not in_grace:
                return self._close(symbol, price, "trailing_stop_exit", timestamp)
            return None

        move = move_pct(pos.direction, pos.avg_price, price)

        if move >= self.grid_pct:
            if self.trail_stop:
                pos.trailing = True
                pos.peak_price = price
                pos.trailing_activated_at = timestamp
                return None
            return self._close(symbol, price, "target_exit", timestamp)

        if not pos.averaged and move <= -self.grid_pct and self.capital_units_available >= 1:
            qty = self.exposure_per_unit / price
            pos.legs.append((price, qty))
            pos.averaged = True
            self.capital_units_available -= 1

        return None

    def check_loss_cap(self, current_prices: dict[str, float], timestamp) -> list[dict]:
        """Mark-to-market: realized + unrealized P&L on open positions. Flattens
        everything and halts new entries/averaging the moment it's breached."""
        if self.halted or not self.open_positions:
            return []
        unrealized = sum(
            pnl(pos.direction, pos.avg_price, current_prices.get(sym, pos.avg_price), pos.qty)
            for sym, pos in self.open_positions.items()
        )
        if self.daily_pnl + unrealized <= -self.daily_loss_cap:
            self.halted = True
            return [
                self._close(sym, current_prices.get(sym, self.open_positions[sym].avg_price), "daily_loss_cap", timestamp)
                for sym in list(self.open_positions.keys())
            ]
        return []

    def square_off(self, current_prices: dict[str, float], timestamp) -> list[dict]:
        return [
            self._close(sym, current_prices.get(sym, self.open_positions[sym].avg_price), "square_off", timestamp)
            for sym in list(self.open_positions.keys())
        ]
