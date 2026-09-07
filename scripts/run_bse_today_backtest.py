"""Single-day backtest: BSE (BSE Ltd) LONG, today only.

Requested 2026-09-07 ("do a backtest for bse today long"). Same shape as
scripts/run_todays_picks_backtest.py -- ONE dated plan, not a repeating
rule -- but it also runs today twice, under two different engine configs,
because the backtest module's defaults and what live.py actually trades
have drifted apart:

  "backtest-default"  what run_backtest() defaults to and what every other
                      script in this repo has been reporting: averaging on,
                      profit exit on, ATR trailing armed at 1.5%.
  "live-config"       what src/live.py would actually have done today:
                      ENABLE_AVERAGING=False, PROFIT_EXIT=False,
                      TRAIL_STOP=False (nothing takes profit at all -- the
                      position rides to square-off unless the daily loss cap
                      or the portfolio profit lock closes it).

The second one is the number that answers "what would my bot have done";
the first is what's comparable to the earlier backtests in this repo.
"""

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src import backtest
from src.strategy import (
    AVERAGING_PCT,
    DEFAULT_ATR_MULTIPLIER,
    ENABLE_AVERAGING,
    GRID_PCT,
    LEVERAGE,
    PORTFOLIO_PROFIT_LOCK_GIVEBACK,
    PORTFOLIO_PROFIT_LOCK_TRIGGER,
    PREMARKET_TRANCHE_PCT,
    PROFIT_EXIT,
    TOTAL_UNITS,
    TRAIL_STOP,
)

SYMBOL = "BSE"  # BSE Ltd on NSE. Same tradingsymbol on Kite and (as BSE.NS) Yahoo.
DIRECTION = "long"
GOAL_DAILY_PNL = 2_000
MARGIN_CAPITAL = backtest.MARGIN_CAPITAL  # Rs 50,000 -- backtest default, not a live balance

# Live sizing for a single pre-market pick, reproduced through run_backtest's
# margin_capital/total_units knobs: live.py hands the pre-market tranche
# (PREMARKET_TRANCHE_PCT of capital) to the stocks known before 9:15, split
# evenly, so ONE pick gets 50% of capital at LEVERAGE = Rs 1.25L exposure.
# run_backtest sizes a unit as (margin_capital / total_units) * leverage, so
# total_units=2 on the same Rs 50,000 reproduces exactly that exposure.
LIVE_TOTAL_UNITS = round(1 / PREMARKET_TRANCHE_PCT)

CAVEATS = """
CAVEATS (read before acting on these numbers):
- ONE trading day and ONE stock. This says what happened today, and nothing
  at all about whether the pick or the strategy is any good.
- Default data source here is Yahoo (src/yahoo_intraday.py), NOT the Kite feed
  the bot actually trades on -- it needs no same-day login, which is the only
  reason it's the default for a quick after-the-fact check. Prices come from
  a different vendor and the last bar or two of the session can be missing,
  so small differences vs Kite are expected. Re-run with --source kite (after
  python3 -m src.auth) for the same feed as live/shadow mode.
- Entry is at today's real 9:15 open, one unit, no entry filter -- the
  backtest assumes the decision to be long BSE today was already made.
- Trigger levels are evaluated on each 5-min bar's CLOSE, not tick by tick,
  so intrabar spikes through a level are invisible here.
- Under the backtest-default config only ONE averaging leg is modeled
  (entry + one add-on), not a multi-level grid. Under live-config averaging
  is off entirely.
- Leverage is a flat 5x on margin -- verify actual Zerodha MIS leverage for
  BSE via Kite's margin calculator before sizing anything real.
- Transaction costs ARE modeled (brokerage, STT, exchange charges, SEBI
  charges, stamp duty, GST) on the leveraged order value. Slippage and
  impact are NOT -- a real Rs 1.25L market order pays some of both.
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--source",
        choices=("yahoo", "kite"),
        default="yahoo",
        help="Bar source. 'kite' is the same feed the bot trades on but needs a same-day login; "
        "'yahoo' (default) needs no login.",
    )
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=date.today(),
        help="Trading day to simulate, YYYY-MM-DD (default: today). Yahoo only keeps ~60 days of 5-min bars.",
    )
    return parser.parse_args()


def load_bars(source: str, day: date) -> tuple[dict, dict]:
    if source == "kite":
        from src import kite_data

        # A window, not a single day: Kite's intraday endpoint is fetched in
        # chunks and `day` still has to fall inside it.
        bars = kite_data.fetch_many([SYMBOL], days=10, interval="5minute")
        return bars, kite_data.fetch_symbol_atr([SYMBOL])

    from src import yahoo_intraday

    lookback_days = max((date.today() - day).days + 5, 5)
    bars = yahoo_intraday.fetch_many([SYMBOL], period=f"{lookback_days}d", interval="5m")
    return bars, yahoo_intraday.fetch_symbol_atr([SYMBOL])


def print_trades(results: dict) -> None:
    trades = pd.DataFrame(results["trade_log"])
    if trades.empty:
        print("    (no trades -- see the daily note above for why)")
        return
    for _, tr in trades.iterrows():
        print(
            f"    {tr['symbol']:8s} {tr['direction']:5s} entry={tr['entry_time']:%H:%M} exit={tr['exit_time']:%H:%M} "
            f"avg={tr['avg_price']:.2f} exit={tr['exit_price']:.2f} qty={tr['qty']:.1f} units={tr['units_used']} "
            f"gross={tr['gross_pnl']:>9,.2f} costs={tr['costs']:>6,.2f} net={tr['pnl']:>9,.2f} ({tr['reason']})"
        )


def main() -> None:
    args = parse_args()
    day = args.date
    daily_plan = {day: {SYMBOL: DIRECTION}}

    print(f"Fetching 5-min bars for {SYMBOL} from {args.source} ...")
    bars, symbol_atr = load_bars(args.source, day)
    if SYMBOL not in bars:
        print(f"No data fetched for {SYMBOL}. Aborting.")
        sys.exit(1)

    day_bars = bars[SYMBOL][bars[SYMBOL].index.date == day]
    if day_bars.empty:
        print(
            f"No bars for {day} -- a holiday/weekend, a day outside the source's history window, "
            "or today's session hasn't produced data yet. Nothing to simulate."
        )
        sys.exit(1)

    atr_note = f"ATR(14d) = {symbol_atr[SYMBOL]:.2f}" if SYMBOL in symbol_atr else "ATR unavailable (percentage trail)"
    print(
        f"{len(day_bars)} bars for {day}: open {day_bars.iloc[0]['Open']:.2f} "
        f"high {day_bars['High'].max():.2f} low {day_bars['Low'].min():.2f} "
        f"last {day_bars.iloc[-1]['Close']:.2f} (to {day_bars.index[-1]:%H:%M}). {atr_note}"
    )

    configs = {
        "backtest-default (averaging ON, profit exit ON, ATR trail)": dict(
            grid_pct=GRID_PCT,
            symbol_atr=symbol_atr,
            atr_multiplier=DEFAULT_ATR_MULTIPLIER,
            margin_capital=MARGIN_CAPITAL,
        ),
        "live-config (what live.py would do: no averaging, no target, no trail)": dict(
            grid_pct=GRID_PCT,
            symbol_atr=symbol_atr,
            atr_multiplier=DEFAULT_ATR_MULTIPLIER,
            averaging_pct=AVERAGING_PCT,
            enable_averaging=ENABLE_AVERAGING,
            trail_stop=TRAIL_STOP,
            profit_exit=PROFIT_EXIT,
            portfolio_profit_lock_trigger=PORTFOLIO_PROFIT_LOCK_TRIGGER,
            portfolio_profit_lock_giveback=PORTFOLIO_PROFIT_LOCK_GIVEBACK,
            margin_capital=MARGIN_CAPITAL,
            total_units=LIVE_TOTAL_UNITS,
            max_concurrent_positions=1,
        ),
    }

    for label, kwargs in configs.items():
        exposure = (kwargs["margin_capital"] / kwargs.get("total_units", TOTAL_UNITS)) * LEVERAGE
        print(f"\n{'=' * 78}\n{day}  {SYMBOL} {DIRECTION.upper()}  --  {label}")
        print(f"Rs {kwargs['margin_capital']:,} margin, {LEVERAGE}x, Rs {exposure:,.0f} exposure on the entry leg\n{'=' * 78}")
        results = backtest.run_backtest(bars, daily_plan=daily_plan, **kwargs)
        print(backtest.summarize(results, goal_daily_pnl=GOAL_DAILY_PNL))
        print("--- Trades ---")
        print_trades(results)

    print(CAVEATS)


if __name__ == "__main__":
    main()
