from dataclasses import dataclass, field
from datetime import time, timedelta

MARGIN_CAPITAL = 50_000  # DEFAULT/fallback only -- live.py and shadow.py size off the
# account's actual available cash each day instead; this is what backtests use.
TOTAL_UNITS = 4
MARGIN_PER_UNIT = MARGIN_CAPITAL / TOTAL_UNITS  # 12,500
LEVERAGE = 5  # Zerodha MIS intraday leverage on equity; varies per stock in reality
MAX_CONCURRENT_POSITIONS = 3  # DEFAULT/fallback -- this is what backtest.py uses.
MAX_STOCKS_PER_DAY = 20  # sanity ceiling to catch a typo/fat-fingered plan file, not a real
# business limit -- capital splits evenly across however many stocks are actually given, so
# there's no fixed cap tied to a specific capital amount; just be aware that more stocks
# means thinner per-stock capital, and per-order costs eat a bigger share of a smaller position
LIVE_CONCURRENT_SLOTS = 10  # requested 2026-08-28: live.py/shadow.py now reserve this many
# slots from the FIRST confirmation of the day, regardless of how many stocks are actually
# given at that point -- not sized dynamically off the initial picks count like before. That
# dynamic sizing meant adding a stock past the original count (CONCOR, ADANIGREEN on
# 2026-08-28) needed a full session restart just to raise the slot count. Fixing the cap at
# 10 up front means a slot freeing up (a position hitting target) always has room for a new
# pick without a restart. Tradeoff: exposure_per_unit is now sized as if up to 10 positions
# could be open at once even on a day with only 2-3 picks, so each position starts smaller
# than the old dynamic sizing gave it.
PREMARKET_TRANCHE_PCT = 0.5  # adopted as the standing default 2026-08-28. Capital is split
# in two: this fraction is reserved for whatever picks are given BEFORE market open (split
# evenly among them), and the rest is reserved for anything added after open (split evenly
# across the remaining LIVE_CONCURRENT_SLOTS capacity). Backtested on 2026-08-28's real
# picks: this let every stock actually get filled (smaller per-order size means far less
# chance of a margin rejection like CONCOR hit that day) and roughly matched or beat the
# single-pool sizing in every trade -- see grid_pct_and_costs memory for the comparison.
PER_STOCK_STOP_LOSS = 2_000  # turned on as a live default 2026-08-28: closes a single
# position outright once its own unrealized loss exceeds this, independent of averaging
# state. Closes exactly the gap that let STARCEMENT ride to square-off (-1996 to -2311
# unrealized over the course of that day) once its one averaging leg didn't recover.
# -1500 was backtest-tested and shown to cut some genuine recoveries too eagerly; -2000
# is untested against real data but would have caught STARCEMENT's worst points -- revisit
# once more real days exist under it.
TRAIL_STOP = False  # turned off 2026-09-01, then superseded by PROFIT_EXIT below
# the same day once it became clear a plain target_exit at GRID_PCT (what trail_stop
# =False actually does) wasn't what was wanted either -- COALINDIA hit target and
# closed in 2 minutes on 2026-09-02, which is direct target_exit behavior working
# exactly as coded, but not the intent. Kept as False since PROFIT_EXIT=False now
# skips this setting's branch entirely anyway.
PROFIT_EXIT = False  # turned off as a live default 2026-09-02: NO automatic
# profit-taking at all -- no fixed target, no trailing arm. A position now only
# ever closes via square_off (3:15/3:30 PM), daily_loss_cap, per_stock_stop_loss
# (if enabled), or portfolio_profit_lock. It rides the full move for better or
# worse. Requested explicitly ("NO TARGET TO BE SET, NO TRAILING SL") after
# COALINDIA's 2-minute target_exit on 2026-09-02 showed that trail_stop=False
# alone (a fixed 1.5% target) still wasn't "no exit strategy" -- this is the
# actual "let it ride" setting. Real risk: an open position between now and
# square-off has NOTHING protecting its own downside except the whole-portfolio
# daily_loss_cap -- a single bad name can ride uncushioned all the way to
# square-off as long as the total account P&L stays above -3,000.
GRID_PCT = 0.015  # revised from 2% on 2026-08-25 -- see grid_pct_and_costs memory for the backtest comparison
# Trailing-stop/target activation threshold -- GRID_PCT above.
AVERAGING_PCT = 0.01  # split off from GRID_PCT on 2026-08-26: averaging now fires on a smaller
# adverse move (1%) than the profit side needs to arm trailing (1.5%) -- previously both used
# the same GRID_PCT value.
ENABLE_AVERAGING = False  # turned off as a live default 2026-08-28. STARCEMENT (averaged
# once, never recovered, -Rs 2,090 real) was the deciding case -- a position that uses its
# one averaging leg and still doesn't recover currently has NO further automated protection
# (per-stock stop-loss was tested and reverted the same day; trailing/profit-lock can't help
# since they only ever arm once a position is genuinely in profit). Backtests earlier in the
# session showed averaging net-positive on the accumulated data overall, so this is a
# deliberate tradeoff -- giving up averaging's upside on days it works to remove the downside
# risk of a position doubling down into a real decline. Revisit with more real days either way.
DAILY_LOSS_CAP = 3_000  # standing default at/under DAILY_LOSS_CAP_BASE_CAPITAL -- see
# compute_daily_loss_cap() below. Replaced the old 20%-of-capital cap (Rs 10,000 on a
# Rs 50,000 day, Rs 20,000 on a Rs 1L day) on 2026-09-01 after a real Rs 5,000 loss day --
# that cap was far too loose to actually stop a bad day early. Requested explicitly:
# floor of Rs 3,000 for capital <= Rs 1L, scaling up Rs 500 per extra Rs 50,000 of capital
# (Rs 3,500 at Rs 1.5L, Rs 4,000 at Rs 2L) so the cap stays roughly proportional to risk
# as the account grows without being as loose as the old 20% ratio.
DAILY_LOSS_CAP_BASE_CAPITAL = 100_000  # capital at/under which the cap is pinned to the floor
DAILY_LOSS_CAP_STEP = 500  # cap increases by this much per DAILY_LOSS_CAP_STEP_CAPITAL of
# capital above the base -- e.g. Rs 150,000 -> 3,000 + 500 = Rs 3,500
DAILY_LOSS_CAP_STEP_CAPITAL = 50_000


def compute_daily_loss_cap(margin_capital: float) -> float:
    """Rs 3,000 floor for any capital <= Rs 1L; above that, +Rs 500 per +Rs 50,000 of
    capital. E.g. Rs 50k/1L -> 3,000, Rs 1.5L -> 3,500, Rs 2L -> 4,000."""
    if margin_capital <= DAILY_LOSS_CAP_BASE_CAPITAL:
        return DAILY_LOSS_CAP
    excess = margin_capital - DAILY_LOSS_CAP_BASE_CAPITAL
    return DAILY_LOSS_CAP + (excess / DAILY_LOSS_CAP_STEP_CAPITAL) * DAILY_LOSS_CAP_STEP
PORTFOLIO_PROFIT_LOCK_TRIGGER = 3000  # raised 700 -> 1000 -> 3000 through 2026-08-28; the
# 3000 figure is now the standing default, not a one-day-only setting. Once
# total (realized + unrealized) day P&L first crosses this, arm and start trailing the peak.
PORTFOLIO_PROFIT_LOCK_GIVEBACK = 500  # raised from 300 alongside the trigger. If total P&L
# then pulls back this much from its peak after arming, everything closes immediately.
# Genuine trailing stop on the whole day's P&L, not a fixed floor -- the lock level itself
# ratchets up as the peak grows. Set either to None (in the GridEngine call) to disable;
# not backtest-proven -- see the sweeps in grid_pct_and_costs memory for why the backtest
# evidence was thin before this was turned on.
SQUARE_OFF_TIME = time(15, 8)  # moved earlier from 15:12 on 2026-08-26 -- Kite itself REJECTS MIS orders
# placed AT or after 15:12 ("Intraday orders (MIS) are allowed only till 3:12 PM"), so a square-off
# attempt landing exactly at 15:12 is already too late. On 2026-08-26 this happened for real -- all
# three real square-off orders were rejected, and the positions only closed because Zerodha's own
# broker-side auto square-off caught them a few minutes later. That's not something to rely on.
DEFAULT_ATR_MULTIPLIER = 0.5  # trail distance = this * symbol's 14-day ATR
# Lowered back to 0.5 on 2026-08-25, reversing the 2026-08-15 change to 1.0x.
# Retested against the (now 1.5%, was 2%) grid activation threshold on the
# accumulated real-day backtest: 0.5x beat 1.0x on avg P&L/day (Rs 2,855 vs
# Rs 2,126), win rate (75% vs 58%) and win days (5/6 vs 4/6), with no worse
# worst-day. Caveat: only 6 real trading days in this comparison, versus 34
# trade cases behind the original 1.0x decision -- see grid_pct_and_costs
# memory for both comparisons; worth re-checking again once more days exist.

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
        margin_capital: float = MARGIN_CAPITAL,
        leverage: float = LEVERAGE,
        daily_loss_cap: float | None = None,
        trail_stop: bool = True,
        profit_exit: bool = True,
        trail_pct: float | None = None,
        atr_multiplier: float | None = None,
        trail_grace_minutes: float = 0,
        lock_in_profit: bool = True,
        total_units: int = TOTAL_UNITS,
        max_concurrent_positions: int = MAX_CONCURRENT_POSITIONS,
        averaging_pct: float | None = None,
        trailing_activation_pct: float | None = None,
        enable_averaging: bool = True,
        per_stock_stop_loss: float | None = None,
        portfolio_profit_lock_trigger: float | None = None,
        portfolio_profit_lock_giveback: float | None = None,
    ):
        self.grid_pct = grid_pct
        # None means "not explicitly overridden" -- defaults to grid_pct so existing callers
        # (e.g. backtest.py) that only pass grid_pct keep their exact prior behavior.
        self.averaging_pct = averaging_pct if averaging_pct is not None else grid_pct
        # None means "not explicitly overridden" -- defaults to grid_pct so existing callers
        # keep their exact prior behavior. Separate from grid_pct so trailing can be tested
        # at a lower activation threshold without touching the profit-lock floor's meaning.
        self.trailing_activation_pct = trailing_activation_pct if trailing_activation_pct is not None else grid_pct
        self.enable_averaging = enable_averaging
        self.apply_costs = apply_costs
        self.margin_capital = margin_capital
        self.total_units = total_units
        self.max_concurrent_positions = max_concurrent_positions
        self.exposure_per_unit = (margin_capital / total_units) * leverage
        # None means "not explicitly overridden" -- derive from margin_capital so the
        # cap scales with real capital instead of staying pinned to the Rs 50,000 default.
        self.daily_loss_cap = daily_loss_cap if daily_loss_cap is not None else compute_daily_loss_cap(margin_capital)
        self.trail_stop = trail_stop
        self.profit_exit = profit_exit
        self.trail_pct = trail_pct if trail_pct is not None else grid_pct / 2
        self.atr_multiplier = atr_multiplier  # if set, trail distance = atr_multiplier * position's ATR (price units) instead of trail_pct
        self.trail_grace_minutes = trail_grace_minutes  # no trailing-stop exit allowed this long after trailing activates
        self.lock_in_profit = lock_in_profit  # once trailing arms, stop level can't fall back below the grid_pct profit floor
        self.capital_units_available = total_units
        self.open_positions: dict[str, Position] = {}
        self.daily_pnl = 0.0
        self.halted = False
        self.trade_log: list[dict] = []
        # Rupee amount -- a single position's own unrealized loss exceeding this
        # closes just that one position, independent of averaging/trailing state.
        # Protects against exactly the "averaged once, still stuck red, nothing
        # left to close it until square-off" case (STARCEMENT, 2026-08-28).
        self.per_stock_stop_loss = per_stock_stop_loss
        # Rupee amount -- once total (realized + unrealized) P&L across the whole
        # day first crosses this, the day's profit gets a floor: if total P&L
        # ever falls back down to this same trigger level after arming, every
        # open position closes immediately to lock it in. Upside is never
        # capped -- only armed once, then only fires on the pullback.
        self.portfolio_profit_lock_trigger = portfolio_profit_lock_trigger
        self.portfolio_profit_lock_giveback = portfolio_profit_lock_giveback
        self.portfolio_profit_lock_armed = False
        self.portfolio_profit_lock_peak = 0.0

    def can_enter(self, symbol: str) -> bool:
        return (
            not self.halted
            and symbol not in self.open_positions
            and len(self.open_positions) < self.max_concurrent_positions
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
            # Pure peak-relative trailing can still let a position fall back below
            # entry: if the pullback from peak exceeds the (smaller) gap between
            # peak and entry, the trail fires below breakeven even though the
            # position was genuinely up grid_pct at some point. lock_price is the
            # floor that prevents that -- once armed, the effective stop can only
            # ratchet UP toward the peak, never back down past the level that
            # locks in the original grid_pct move.
            lock_price = (
                pos.avg_price * (1 + self.trailing_activation_pct)
                if pos.direction == "long"
                else pos.avg_price * (1 - self.trailing_activation_pct)
            )
            if pos.direction == "long":
                pos.peak_price = max(pos.peak_price, price)
                trail_level = pos.peak_price - self.atr_multiplier * pos.atr if use_atr else pos.peak_price * (1 - self.trail_pct)
                stop_level = max(trail_level, lock_price) if self.lock_in_profit else trail_level
                triggered = price <= stop_level
            else:
                pos.peak_price = min(pos.peak_price, price)
                trail_level = pos.peak_price + self.atr_multiplier * pos.atr if use_atr else pos.peak_price * (1 + self.trail_pct)
                stop_level = min(trail_level, lock_price) if self.lock_in_profit else trail_level
                triggered = price >= stop_level
            in_grace = (
                self.trail_grace_minutes > 0
                and pos.trailing_activated_at is not None
                and (timestamp - pos.trailing_activated_at) < timedelta(minutes=self.trail_grace_minutes)
            )
            if triggered and not in_grace:
                return self._close(symbol, price, "trailing_stop_exit", timestamp)
            return None

        move = move_pct(pos.direction, pos.avg_price, price)

        # Trailing arms at trailing_activation_pct (may differ from grid_pct); a direct
        # target exit (trail_stop=False) still uses grid_pct, its original meaning.
        # profit_exit=False skips this whole block -- no target, no trailing arm --
        # the position rides until square_off, daily_loss_cap, per_stock_stop_loss,
        # or portfolio_profit_lock closes it. Nothing takes profit on its own.
        if self.profit_exit:
            arm_threshold = self.trailing_activation_pct if self.trail_stop else self.grid_pct
            if move >= arm_threshold:
                if self.trail_stop:
                    pos.trailing = True
                    pos.peak_price = price
                    pos.trailing_activated_at = timestamp
                    return None
                return self._close(symbol, price, "target_exit", timestamp)

        if self.enable_averaging and not pos.averaged and move <= -self.averaging_pct and self.capital_units_available >= 1:
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

    def check_per_stock_stop_loss(self, current_prices: dict[str, float], timestamp) -> list[dict]:
        """Close any single open position whose own unrealized loss exceeds
        per_stock_stop_loss, regardless of averaging/trailing state -- the one
        protection a position has left once it's already used its one
        averaging leg and still hasn't recovered (see STARCEMENT, 2026-08-28:
        no further automatic exit existed for a position stuck red after
        averaging, right up until square-off)."""
        if self.halted or not self.open_positions or self.per_stock_stop_loss is None:
            return []
        closed = []
        for sym in list(self.open_positions.keys()):
            pos = self.open_positions[sym]
            price = current_prices.get(sym, pos.avg_price)
            unrealized = pnl(pos.direction, pos.avg_price, price, pos.qty)
            if unrealized <= -self.per_stock_stop_loss:
                closed.append(self._close(sym, price, "per_stock_stop_loss", timestamp))
        return closed

    def check_portfolio_profit_lock(self, current_prices: dict[str, float], timestamp) -> list[dict]:
        """Once total (realized + unrealized) P&L first crosses
        portfolio_profit_lock_trigger, arm and start tracking the peak total
        P&L reached since. If total P&L then pulls back
        portfolio_profit_lock_giveback (rupees) from that peak, close
        everything immediately. The floor itself ratchets up as the peak
        grows -- this is a genuine trailing stop on the whole day's P&L, not
        a fixed floor at the trigger level. Never caps the upside on its own;
        only fires on the pullback. Halts further entries once triggered,
        same as the loss cap, since a profit has already been locked in."""
        if self.halted or not self.open_positions or self.portfolio_profit_lock_trigger is None:
            return []
        unrealized = sum(
            pnl(pos.direction, pos.avg_price, current_prices.get(sym, pos.avg_price), pos.qty)
            for sym, pos in self.open_positions.items()
        )
        total = self.daily_pnl + unrealized

        if not self.portfolio_profit_lock_armed:
            if total >= self.portfolio_profit_lock_trigger:
                self.portfolio_profit_lock_armed = True
                self.portfolio_profit_lock_peak = total
            return []

        self.portfolio_profit_lock_peak = max(self.portfolio_profit_lock_peak, total)
        giveback = self.portfolio_profit_lock_giveback if self.portfolio_profit_lock_giveback is not None else 0.0
        # Floor never drops below the original trigger itself -- it only ratchets UP once the
        # peak grows past trigger + giveback. Same "floor" pattern as the per-position profit
        # lock: 700 is the guaranteed worst case once armed, not just a starting point giveback
        # can erode below.
        floor = max(self.portfolio_profit_lock_peak - giveback, self.portfolio_profit_lock_trigger)
        if total <= floor:
            self.halted = True
            return [
                self._close(sym, current_prices.get(sym, self.open_positions[sym].avg_price), "portfolio_profit_lock", timestamp)
                for sym in list(self.open_positions.keys())
            ]
        return []

    def square_off(self, current_prices: dict[str, float], timestamp) -> list[dict]:
        return [
            self._close(sym, current_prices.get(sym, self.open_positions[sym].avg_price), "square_off", timestamp)
            for sym in list(self.open_positions.keys())
        ]
