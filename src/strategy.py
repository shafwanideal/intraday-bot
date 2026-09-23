from dataclasses import dataclass, field
from datetime import time, timedelta

MARGIN_CAPITAL = 50_000  # DEFAULT/fallback only -- live.py and shadow.py size off the
# account's actual available cash each day instead; this is what backtests use.
TOTAL_UNITS = 4
MARGIN_PER_UNIT = MARGIN_CAPITAL / TOTAL_UNITS  # 12,500
LEVERAGE = 5  # reverted 2026-09-17 (explicit user request) from the 1x set on 2026-09-13.
# The daily loss cap and portfolio profit floor (compute_daily_loss_cap,
# compute_portfolio_profit_lock_trigger) are both a % of raw margin_capital, NOT of
# leveraged exposure -- deliberately unchanged by this revert. That's a fixed rupee
# risk budget ("how much am I willing to lose today"), independent of how much
# leverage is used to get there -- but it does mean that budget now burns through
# with roughly 1/5th the price movement it used to, since positions are 5x larger
# for the same % allocation. Zerodha's real per-stock MIS multiplier varies and
# isn't guaranteed to be exactly 5x regardless of what this constant says.
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
TRAIL_STOP = True  # restored as a live default 2026-09-11, reversing the 2026-09-02
# "let it ride" decision below -- explicitly requested again: arm a trailing stop
# once a position moves GRID_PCT (1.5%) in its favor, rather than no exit strategy
# at all. Kept the full history of the prior decision below since the tradeoff it
# describes (no downside protection between arming and square-off) is exactly what
# this reversal is choosing to accept differently.
PROFIT_EXIT = True  # restored 2026-09-11 alongside TRAIL_STOP above, for the same
# reason -- profit_exit=False (below) skips the entire trailing/target block, so
# both flags have to flip together to actually get a trailing stop back.
# --- history: why this was turned off 2026-09-02, kept for context ---
# Turned off as a live default 2026-09-02: NO automatic
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
TRAILING_ACTIVATION_PCT = 0.01  # lowered from GRID_PCT (1.5%) on 2026-09-22 -- real case,
# HDFCBANK moved +1.08% and reversed back near breakeven the same day without the trailing
# stop ever arming, since it never reached 1.5%. At 1%, the same move would have armed
# protection and locked in a real gain instead of giving the whole move back. trail_pct
# (how far behind the peak it trails once armed, 0.75% by default) is unchanged -- this
# only controls how SOON protection turns on, not how much room it gives afterward.
TRAILING_ACTIVATION_PCT_AFTER_NOON = 0.005  # added 2026-09-23: tighten the arm threshold
# to 0.5% from 12:00 IST onward. A position that entered (or is still riding, unarmed)
# in the afternoon has less of the day's move left ahead of it than one that entered at
# the open, so waiting for the full 1% move before arming protection gives back more of
# whatever afternoon gain there is. Only affects positions that haven't armed yet at the
# moment of the check -- a position already trailing before noon keeps the activation pct
# (and therefore the lock-in floor) it originally armed at; see Position.armed_activation_pct.
TRAILING_ACTIVATION_CUTOVER_TIME = time(12, 0)  # IST
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
DAILY_LOSS_CAP = 3_000  # DEFAULT/fallback only, at MARGIN_CAPITAL -- live.py/shadow.py
# compute the real cap fresh each day via compute_daily_loss_cap(actual margin_capital).
# --- history, kept for context: the stepped-floor formula this replaced 2026-09-13 ---
# Was: Rs 3,000 floor for capital <= Rs 1L, +Rs 500 per +Rs 50,000 above that (Rs 3,500 at
# 1.5L, Rs 4,000 at 2L) -- itself adopted 2026-09-01 after a real Rs 5,000 loss day showed
# the OLDER 20%-of-capital cap (Rs 10,000 on a Rs 50,000 day) far too loose to stop a bad
# day early. DAILY_LOSS_CAP_BASE_CAPITAL/STEP/STEP_CAPITAL below are unused now but left
# in case that stepped shape is ever wanted again.
DAILY_LOSS_CAP_BASE_CAPITAL = 100_000
DAILY_LOSS_CAP_STEP = 500
DAILY_LOSS_CAP_STEP_CAPITAL = 50_000
DAILY_LOSS_CAP_PCT = 0.04  # 2026-09-13: the layered-trailing-stop/portfolio-floor spec
# calls for a straight -4% of the day's fund, scaling linearly with capital (Rs 3,000 on
# Rs 75,000, Rs 4,000 on Rs 1L) -- replacing the stepped-floor shape above, which was NOT
# proportional at every capital level (e.g. Rs 3,000 on Rs 50,000 is 6%, not 4%).
# Known real-world gap, proven in testing: this is only checked every 5-min bar close in
# backtest and every poll interval live -- a fast/violent move can overshoot it before the
# close/exit fires. Budget roughly Rs 3,000-3,500 worst case on a Rs 75,000 fund, not an
# exact ceiling.


def compute_daily_loss_cap(margin_capital: float) -> float:
    """Straight DAILY_LOSS_CAP_PCT (4%) of the day's actual fund. Replaced the old
    stepped-floor shape 2026-09-13 -- see history above compute_daily_loss_cap's
    constants for why."""
    return DAILY_LOSS_CAP_PCT * margin_capital


PORTFOLIO_PROFIT_LOCK_TRIGGER = 3000  # DEFAULT/fallback only, at MARGIN_CAPITAL --
# live.py/shadow.py compute the real trigger fresh each day via
# compute_portfolio_profit_lock_trigger(actual margin_capital).
PORTFOLIO_PROFIT_LOCK_TRIGGER_PCT = 0.02667  # 2026-09-13: the layered-trailing-stop
# spec calls for the profit floor to arm at 2.667% of the day's fund (e.g. Rs 2,000 on
# Rs 75,000), not a fixed rupee figure that stays Rs 3,000 regardless of capital.


def compute_portfolio_profit_lock_trigger(margin_capital: float) -> float:
    """Straight PORTFOLIO_PROFIT_LOCK_TRIGGER_PCT (2.667%) of the day's actual fund."""
    return PORTFOLIO_PROFIT_LOCK_TRIGGER_PCT * margin_capital


DAILY_PROFIT_TARGET_PCT = 0.0533  # 2026-09-18 request: a hard daily take-profit ceiling,
# on top of (not instead of) the portfolio profit lock above. Same 2.667%-on-Rs-75,000
# ratio as the other two (Rs 4,000 on Rs 75,000), scaling with margin_capital rather than
# staying a fixed rupee figure that goes stale as capital changes.
#
# Distinct from portfolio_profit_lock: that one only fires on the way back DOWN once
# armed (it never caps the upside on its own -- see check_portfolio_profit_lock's
# docstring). This fires the moment total P&L first REACHES the target, locking in the
# win immediately rather than waiting for a pullback -- added after a 2026-09-18 real
# session where total P&L peaked near Rs 4,000 unrealized and, by the time the profit
# lock's pullback-triggered exit actually closed out (detection lag + sequential order
# placement), the real realized result was negative. Same overshoot risk applies here
# too: this is checked once per poll, not tick by tick, so the real fill can land a bit
# short of the target on a fast move -- it substantially narrows the gap, it doesn't
# eliminate it.


def compute_daily_profit_target(margin_capital: float) -> float:
    """Straight DAILY_PROFIT_TARGET_PCT (5.33%) of the day's actual fund."""
    return DAILY_PROFIT_TARGET_PCT * margin_capital


PORTFOLIO_PROFIT_LOCK_GIVEBACK = 500  # only meaningful when portfolio_profit_lock_fixed=False
# (the original ratcheting behavior -- see GridEngine.check_portfolio_profit_lock). If total
# P&L pulls back this much from its peak after arming, everything closes; the lock level
# itself ratchets up as the peak grows. raised from 300 alongside the trigger through
# 2026-08-28; not backtest-proven on its own -- see the sweeps in grid_pct_and_costs memory.
# 2026-09-13: the layered-trailing-stop spec instead wants a FIXED floor pinned at the
# arming value forever (no ratchet at all) -- see portfolio_profit_lock_fixed=True, which
# live.py/shadow.py now pass, making this giveback value irrelevant to them.
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
    armed_activation_pct: float | None = None  # the trailing_activation_pct in effect at the
    # moment THIS position armed -- fixed at arm time so a later time-based threshold change
    # (see TRAILING_ACTIVATION_PCT_AFTER_NOON) doesn't retroactively move an already-armed
    # position's lock-in floor.

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
        trailing_activation_pct_after: float | None = None,
        trailing_activation_cutover_time: time | None = None,
        enable_averaging: bool = True,
        per_stock_stop_loss: float | None = None,
        portfolio_profit_lock_trigger: float | None = None,
        portfolio_profit_lock_giveback: float | None = None,
        portfolio_profit_lock_fixed: bool = False,
        daily_profit_target: float | None = None,
    ):
        self.grid_pct = grid_pct
        # None means "not explicitly overridden" -- defaults to grid_pct so existing callers
        # (e.g. backtest.py) that only pass grid_pct keep their exact prior behavior.
        self.averaging_pct = averaging_pct if averaging_pct is not None else grid_pct
        # None means "not explicitly overridden" -- defaults to grid_pct so existing callers
        # keep their exact prior behavior. Separate from grid_pct so trailing can be tested
        # at a lower activation threshold without touching the profit-lock floor's meaning.
        self.trailing_activation_pct = trailing_activation_pct if trailing_activation_pct is not None else grid_pct
        # Both None (the default) means no time-based change -- trailing_activation_pct applies
        # all day, unchanged from before this was added. Setting trailing_activation_pct_after
        # switches the arm threshold to that value from trailing_activation_cutover_time onward,
        # for positions that haven't armed yet -- see _trailing_activation_pct_at.
        self.trailing_activation_pct_after = trailing_activation_pct_after
        self.trailing_activation_cutover_time = trailing_activation_cutover_time
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
        # False (default): the original genuine trailing stop on whole-day P&L -- the
        # floor ratchets up as the peak grows (see check_portfolio_profit_lock).
        # True: a fixed floor pinned at the trigger value forever, never rising further
        # even as profit grows -- the layered-trailing-stop/portfolio-floor spec's
        # "Portfolio Profit Floor (fixed, not ratcheting)" rule, requested 2026-09-13.
        # This is NOT the same as giveback=0 on the ratcheting version: giveback=0 there
        # still ratchets floor=max(peak,trigger) up with the peak, so it fires on the very
        # next tick that isn't itself a new peak -- far too twitchy. Fixed mode ignores
        # the peak (and giveback) entirely once armed.
        self.portfolio_profit_lock_fixed = portfolio_profit_lock_fixed
        self.portfolio_profit_lock_armed = False
        self.portfolio_profit_lock_peak = 0.0
        # Rupee amount -- a hard ceiling, separate from (and checked in addition to)
        # portfolio_profit_lock_trigger above. Fires the instant total P&L first REACHES
        # this level, closing everything immediately -- unlike the profit lock, it does
        # not wait for a pullback. See check_daily_profit_target.
        self.daily_profit_target = daily_profit_target

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

    def _trailing_activation_pct_at(self, timestamp) -> float:
        """The arm threshold in effect for a NOT-YET-armed position at this
        timestamp -- self.trailing_activation_pct all day, unless
        trailing_activation_pct_after/cutover_time are set and timestamp has
        reached the cutover, in which case the (tighter) after-cutover value."""
        if self.trailing_activation_pct_after is not None and self.trailing_activation_cutover_time is not None:
            if timestamp.time() >= self.trailing_activation_cutover_time:
                return self.trailing_activation_pct_after
        return self.trailing_activation_pct

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
            # locks in the original grid_pct move. Uses the activation pct THIS
            # position actually armed at (armed_activation_pct), not whatever
            # is current now -- a later time-based threshold change must not
            # retroactively move an already-armed position's floor.
            activation_pct = pos.armed_activation_pct if pos.armed_activation_pct is not None else self.trailing_activation_pct
            lock_price = (
                pos.avg_price * (1 + activation_pct)
                if pos.direction == "long"
                else pos.avg_price * (1 - activation_pct)
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
            current_activation_pct = self._trailing_activation_pct_at(timestamp)
            arm_threshold = current_activation_pct if self.trail_stop else self.grid_pct
            if move >= arm_threshold:
                if self.trail_stop:
                    pos.trailing = True
                    pos.peak_price = price
                    pos.trailing_activated_at = timestamp
                    pos.armed_activation_pct = current_activation_pct
                    return None
                return self._close(symbol, price, "target_exit", timestamp)

        if self.enable_averaging and not pos.averaged and move <= -self.averaging_pct and self.capital_units_available >= 1:
            qty = self.exposure_per_unit / price
            pos.legs.append((price, qty))
            pos.averaged = True
            self.capital_units_available -= 1

        return None

    def _total_pnl_at(self, prices: dict[str, float]) -> float:
        return self.daily_pnl + sum(
            pnl(pos.direction, pos.avg_price, prices.get(sym, pos.avg_price), pos.qty)
            for sym, pos in self.open_positions.items()
        )

    def _interp_close_all(self, reason: str, target_total: float, open_prices: dict[str, float], extreme_prices: dict[str, float], timestamp) -> list[dict]:
        """Close every open position at a price interpolated along its own
        open->extreme range, at whatever fraction of that range brings the
        COMBINED total P&L to exactly target_total.

        Backtests only see a bar's Open/High/Low/Close, not the real path
        price took between them -- a plain close-price check can badly
        overshoot a threshold if the real move happened mid-bar (see e.g.
        Cochin Shipyard's 2026-09-11 gap-down: a close-only loss cap check
        landed near -13,000 against a -3,000 cap). This assumes the total
        P&L moved roughly linearly from its bar-open value to its bar-extreme
        value and finds where along that path it would have crossed
        target_total, exiting each symbol at the matching point on ITS OWN
        price range -- the same approximation a real intrabar-reactive stop
        order would achieve. Still an approximation (real price paths aren't
        linear, and multiple symbols' real crossings needn't be simultaneous),
        but far closer to reality than only checking the bar's close."""
        total_open = self._total_pnl_at(open_prices)
        total_extreme = self._total_pnl_at(extreme_prices)
        span = total_extreme - total_open
        frac = (target_total - total_open) / span if span != 0 else 1.0
        frac = max(0.0, min(1.0, frac))
        closed = []
        for sym in list(self.open_positions.keys()):
            op = open_prices.get(sym, self.open_positions[sym].avg_price)
            ext = extreme_prices.get(sym, op)
            exit_price = op + frac * (ext - op)
            closed.append(self._close(sym, exit_price, reason, timestamp))
        return closed

    def check_loss_cap(
        self,
        current_prices: dict[str, float],
        timestamp,
        open_prices: dict[str, float] | None = None,
        worst_prices: dict[str, float] | None = None,
    ) -> list[dict]:
        """Mark-to-market: realized + unrealized P&L on open positions. Flattens
        everything and halts new entries/averaging the moment it's breached.

        `open_prices`/`worst_prices` are optional and backtest-only (live/shadow
        never pass them, so their behavior is unchanged): when given, they're
        each bar's Open and worst-case extreme (Low for a long position, High
        for a short) per symbol, letting this catch a breach that happened
        mid-bar via _interp_close_all rather than only at the bar's close --
        see that method's docstring for why this matters.

        Only used with exactly one open position: combining each
        symbol's own worst-case extreme into a portfolio total assumes every
        symbol hits its individual extreme simultaneously, which real,
        unrelated stocks don't do -- it manufactures a portfolio swing far
        bigger than anything that actually happened and can even close a
        winning day at a fabricated loss. With exactly one open position, a
        symbol's own extreme IS the portfolio's extreme, so the assumption is
        exact rather than an approximation -- that single-position case is
        where this still applies."""
        if self.halted or not self.open_positions:
            return []
        if open_prices is not None and worst_prices is not None and len(self.open_positions) == 1:
            total_worst = self._total_pnl_at(worst_prices)
            if total_worst <= -self.daily_loss_cap:
                self.halted = True
                total_open = self._total_pnl_at(open_prices)
                if total_open <= -self.daily_loss_cap:
                    return [
                        self._close(sym, worst_prices.get(sym, self.open_positions[sym].avg_price), "daily_loss_cap", timestamp)
                        for sym in list(self.open_positions.keys())
                    ]
                return self._interp_close_all("daily_loss_cap", -self.daily_loss_cap, open_prices, worst_prices, timestamp)
            return []
        if self._total_pnl_at(current_prices) <= -self.daily_loss_cap:
            self.halted = True
            return [
                self._close(sym, current_prices.get(sym, self.open_positions[sym].avg_price), "daily_loss_cap", timestamp)
                for sym in list(self.open_positions.keys())
            ]
        return []

    def check_daily_profit_target(
        self,
        current_prices: dict[str, float],
        timestamp,
        open_prices: dict[str, float] | None = None,
        best_prices: dict[str, float] | None = None,
    ) -> list[dict]:
        """Mark-to-market: hard ceiling on the whole day's P&L, separate from
        portfolio_profit_lock_trigger. Flattens everything and halts the moment total
        (realized + unrealized) P&L first REACHES daily_profit_target -- it does not
        wait for a pullback the way check_portfolio_profit_lock does. Use both together:
        this locks in the win the instant the target is hit; the profit lock below it
        still protects gains that fall short of this ceiling.

        `open_prices`/`best_prices` are optional and backtest-only (see
        check_loss_cap's docstring for the identical mid-bar rationale, just on
        the profit side here: each symbol's bar Open and best-case extreme,
        High for a long / Low for a short).

        Only used with exactly one open position -- see check_loss_cap's
        docstring for why combining several symbols' own extremes into one
        portfolio total is invalid with more than one position open."""
        if self.halted or not self.open_positions or self.daily_profit_target is None:
            return []
        if open_prices is not None and best_prices is not None and len(self.open_positions) == 1:
            total_best = self._total_pnl_at(best_prices)
            if total_best >= self.daily_profit_target:
                self.halted = True
                total_open = self._total_pnl_at(open_prices)
                if total_open >= self.daily_profit_target:
                    return [
                        self._close(sym, best_prices.get(sym, self.open_positions[sym].avg_price), "daily_profit_target", timestamp)
                        for sym in list(self.open_positions.keys())
                    ]
                return self._interp_close_all("daily_profit_target", self.daily_profit_target, open_prices, best_prices, timestamp)
            return []
        if self._total_pnl_at(current_prices) >= self.daily_profit_target:
            self.halted = True
            return [
                self._close(sym, current_prices.get(sym, self.open_positions[sym].avg_price), "daily_profit_target", timestamp)
                for sym in list(self.open_positions.keys())
            ]
        return []

    def check_per_stock_stop_loss(
        self,
        current_prices: dict[str, float],
        timestamp,
        open_prices: dict[str, float] | None = None,
        worst_prices: dict[str, float] | None = None,
    ) -> list[dict]:
        """Close any single open position whose own unrealized loss exceeds
        per_stock_stop_loss, regardless of averaging/trailing state -- the one
        protection a position has left once it's already used its one
        averaging leg and still hasn't recovered (see STARCEMENT, 2026-08-28:
        no further automatic exit existed for a position stuck red after
        averaging, right up until square-off).

        `open_prices`/`worst_prices` are optional and backtest-only (see
        check_loss_cap's docstring for the mid-bar rationale). Unlike the
        portfolio-level checks, this one never combines symbols -- each
        position is judged only against its own P&L -- so there's no
        simultaneous-extremes assumption to fabricate, and the intrabar
        interpolation below is exact regardless of how many positions are
        open."""
        if self.halted or not self.open_positions or self.per_stock_stop_loss is None:
            return []
        intrabar = open_prices is not None and worst_prices is not None
        closed = []
        for sym in list(self.open_positions.keys()):
            pos = self.open_positions[sym]
            if intrabar and sym in open_prices and sym in worst_prices:
                op, wp = open_prices[sym], worst_prices[sym]
                unrealized_open = pnl(pos.direction, pos.avg_price, op, pos.qty)
                unrealized_worst = pnl(pos.direction, pos.avg_price, wp, pos.qty)
                if unrealized_worst <= -self.per_stock_stop_loss:
                    span = unrealized_worst - unrealized_open
                    frac = (-self.per_stock_stop_loss - unrealized_open) / span if span != 0 else 1.0
                    frac = max(0.0, min(1.0, frac))
                    exit_price = op + frac * (wp - op)
                    closed.append(self._close(sym, exit_price, "per_stock_stop_loss", timestamp))
                continue
            price = current_prices.get(sym, pos.avg_price)
            unrealized = pnl(pos.direction, pos.avg_price, price, pos.qty)
            if unrealized <= -self.per_stock_stop_loss:
                closed.append(self._close(sym, price, "per_stock_stop_loss", timestamp))
        return closed

    def check_portfolio_profit_lock(
        self,
        current_prices: dict[str, float],
        timestamp,
        open_prices: dict[str, float] | None = None,
        best_prices: dict[str, float] | None = None,
        worst_prices: dict[str, float] | None = None,
    ) -> list[dict]:
        """Once total (realized + unrealized) P&L first crosses
        portfolio_profit_lock_trigger, arm.

        Two distinct modes from there, selected by portfolio_profit_lock_fixed:

        - False (default): track the peak total P&L reached since arming. If
          total then pulls back portfolio_profit_lock_giveback (rupees) from
          that peak, close everything -- a genuine trailing stop on the
          whole day's P&L, floor ratchets UP as the peak grows.
        - True: the floor is pinned at the trigger value forever, never
          rising with the peak -- close everything the moment total P&L
          falls back to <= the trigger. Simpler and stricter: it gives up
          all upside past the trigger the instant it stops climbing,
          rather than riding a rising peak down by `giveback`.

        Neither mode caps the upside on its own -- only fires on the
        pullback/fallback. Halts further entries once triggered, same as
        the loss cap, since a profit has already been locked in.

        `open_prices`/`best_prices`/`worst_prices` are optional and
        backtest-only (see check_loss_cap's docstring for the mid-bar
        rationale). When given: arming (and peak tracking in ratcheting mode)
        uses each bar's best-case extreme, so a peak the bar's close alone
        would have missed still gets recognized -- and the breach check
        uses the bar's worst-case extreme with _interp_close_all, instead of
        only the close. This is what fixes a real case found in testing: a
        position arming this trigger then giving almost all of it back
        within the SAME bar closed near breakeven under close-only checking,
        because neither the true peak nor the true breach point were ever
        the bar's close.

        Only used with exactly one open position -- see check_loss_cap's
        docstring for why combining several symbols' own extremes into one
        portfolio total is invalid with more than one position open."""
        if self.halted or not self.open_positions or self.portfolio_profit_lock_trigger is None:
            return []
        intrabar = (
            open_prices is not None and best_prices is not None and worst_prices is not None
            and len(self.open_positions) == 1
        )
        total_best = self._total_pnl_at(best_prices) if intrabar else self._total_pnl_at(current_prices)
        total_worst = self._total_pnl_at(worst_prices) if intrabar else self._total_pnl_at(current_prices)

        if not self.portfolio_profit_lock_armed:
            if total_best >= self.portfolio_profit_lock_trigger:
                self.portfolio_profit_lock_armed = True
                self.portfolio_profit_lock_peak = total_best
            return []

        if self.portfolio_profit_lock_fixed:
            floor = self.portfolio_profit_lock_trigger
        else:
            self.portfolio_profit_lock_peak = max(self.portfolio_profit_lock_peak, total_best)
            giveback = self.portfolio_profit_lock_giveback if self.portfolio_profit_lock_giveback is not None else 0.0
            # Floor never drops below the original trigger itself -- it only ratchets UP once the
            # peak grows past trigger + giveback. Same "floor" pattern as the per-position profit
            # lock: 700 is the guaranteed worst case once armed, not just a starting point giveback
            # can erode below.
            floor = max(self.portfolio_profit_lock_peak - giveback, self.portfolio_profit_lock_trigger)
        if total_worst <= floor:
            self.halted = True
            if intrabar:
                total_open = self._total_pnl_at(open_prices)
                if total_open <= floor:
                    return [
                        self._close(sym, worst_prices.get(sym, self.open_positions[sym].avg_price), "portfolio_profit_lock", timestamp)
                        for sym in list(self.open_positions.keys())
                    ]
                return self._interp_close_all("portfolio_profit_lock", floor, open_prices, worst_prices, timestamp)
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
