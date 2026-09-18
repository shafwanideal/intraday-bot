"""Compares the current live-trading rules against the same rules PLUS the new
2026-09-18 daily_profit_target hard ceiling, replayed over the user's actual
past real trading days (same DAILY_PLAN as run_daily_plan_backtest.py).

Answers: how often would a day have crossed the new Rs 4,000 target at some
point, and what would locking in right there have changed versus what
actually happened (profit lock / loss cap / square-off as today's code
already runs)?

    .venv/bin/python3 scripts/run_profit_target_backtest.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_daily_plan_backtest import DAILY_PLAN, GRID_PCT  # noqa: E402

from src import backtest, kite_data  # noqa: E402
from src.strategy import (  # noqa: E402
    compute_daily_loss_cap,
    compute_daily_profit_target,
    compute_portfolio_profit_lock_trigger,
)

# Matches the real account's actual margin_capital (Rs 2,000 profit-lock arm /
# Rs 3,000 loss cap the user already runs live works out to ~Rs 75,000 -- see
# compute_portfolio_profit_lock_trigger/compute_daily_loss_cap's own comments).
MARGIN_CAPITAL = 75_000

ALL_SYMBOLS = sorted({sym for day_plan in DAILY_PLAN.values() for sym in day_plan})

CAVEATS = """
CAVEATS:
- Both configs below use portfolio_profit_lock_fixed=True (pinned floor, no
  ratchet) to match live.py's real production setting exactly -- backtest.py's
  own default is the ratcheting mode, which is NOT what's running live.
- Same data/grid/averaging/costs caveats as run_daily_plan_backtest.py: Kite
  5-min bars (not tick data), checked once per bar close (not tick by tick --
  same real-world gap that exists live between polls), one averaging leg max,
  transaction costs modeled, slippage not modeled.
- margin_capital is fixed at Rs 75,000 for both configs here (today's real
  account size) rather than re-fetched per historical day -- your real
  capital may have been slightly different on some of these actual dates.
- This replays real price action against the CURRENT rule set applied
  retroactively -- it does not mean these rules were actually running on
  those historical days.
"""


def main() -> None:
    print(f"Fetching intraday data for {ALL_SYMBOLS} from Kite ...")
    bars = kite_data.fetch_many(ALL_SYMBOLS, days=180, interval="5minute")
    missing = [s for s in ALL_SYMBOLS if s not in bars]
    if missing:
        print(f"WARNING: no data for {missing}")

    print("Fetching ATR (14d) for volatility-scaled trailing stop ...")
    symbol_atr = kite_data.fetch_symbol_atr(ALL_SYMBOLS)

    common_kwargs = dict(
        data=bars,
        daily_plan=DAILY_PLAN,
        grid_pct=GRID_PCT,
        symbol_atr=symbol_atr,
        atr_multiplier=backtest.DEFAULT_ATR_MULTIPLIER,
        margin_capital=MARGIN_CAPITAL,
        daily_loss_cap=compute_daily_loss_cap(MARGIN_CAPITAL),
        portfolio_profit_lock_trigger=compute_portfolio_profit_lock_trigger(MARGIN_CAPITAL),
        portfolio_profit_lock_fixed=True,
    )

    baseline = backtest.run_backtest(**common_kwargs, daily_profit_target=None)
    with_target = backtest.run_backtest(**common_kwargs, daily_profit_target=compute_daily_profit_target(MARGIN_CAPITAL))

    print(f"\n{'=' * 90}")
    print(
        f"BASELINE (current live rules: loss cap Rs {common_kwargs['daily_loss_cap']:,.0f}, "
        f"profit lock arms Rs {common_kwargs['portfolio_profit_lock_trigger']:,.0f}, no hard target)"
        f"\nvs WITH TARGET (+ hard ceiling at Rs {compute_daily_profit_target(MARGIN_CAPITAL):,.0f})"
    )
    print(f"{'=' * 90}\n")

    import pandas as pd

    base_daily = pd.DataFrame(baseline["daily_results"]).set_index("date")
    target_daily = pd.DataFrame(with_target["daily_results"]).set_index("date")
    base_trades = pd.DataFrame(baseline["trade_log"])
    target_trades = pd.DataFrame(with_target["trade_log"])

    total_base = 0.0
    total_target = 0.0
    changed_days = 0

    for day in sorted(DAILY_PLAN.keys()):
        symbols_today = ", ".join(f"{s}({d})" for s, d in DAILY_PLAN.get(day, {}).items())
        base_row = base_daily.loc[day] if day in base_daily.index else None
        target_row = target_daily.loc[day] if day in target_daily.index else None
        if base_row is None or isinstance(base_row.get("note"), str):
            print(f"{day}  {symbols_today:55s} [skipped: {base_row.get('note') if base_row is not None else 'no data'}]")
            continue

        base_pnl = base_row["pnl"]
        target_pnl = target_row["pnl"]
        total_base += base_pnl
        total_target += target_pnl

        hit_target = not target_trades.empty and (
            (target_trades["date"] == day) & (target_trades["reason"] == "daily_profit_target")
        ).any()

        marker = ""
        if abs(target_pnl - base_pnl) > 0.01:
            changed_days += 1
            marker = f"  <-- CHANGED by Rs {target_pnl - base_pnl:+,.2f}"
        elif hit_target:
            marker = "  (target reached exactly at square-off/loss-cap price -- no difference)"

        tag = " [TARGET FIRED]" if hit_target else ""
        print(f"{day}  {symbols_today:55s} baseline: Rs {base_pnl:>9,.2f}   with target: Rs {target_pnl:>9,.2f}{tag}{marker}")

    print(f"\n{'-' * 90}")
    print(f"Total baseline P&L:    Rs {total_base:,.2f}")
    print(f"Total with-target P&L: Rs {total_target:,.2f}")
    print(f"Difference:            Rs {total_target - total_base:+,.2f}")
    print(f"Days where the target actually changed the outcome: {changed_days} / {len(DAILY_PLAN)}")

    print(CAVEATS)


if __name__ == "__main__":
    main()
