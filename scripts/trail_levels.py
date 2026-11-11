#!/usr/bin/env python3
"""Where does each trailing-stop variant put the stop on a position I hold now?

Replays one real MTF position day by day from its entry, using the committed
spec's arming rule (high reaches avg x (1 + breakeven% + 2%), breakeven
recomputed daily including accrued interest), and reports, for each trail
variant, whether the stop has armed, where it sits today, and whether the
position would already have been stopped out.

Tiers are chosen from PEAK profit and the stop level is ratcheted, matching
scripts/run_atr_tier_backtest.py.

Usage:
    python3 scripts/trail_levels.py DIVISLAB --entry 2026-11-09
    python3 scripts/trail_levels.py DIVISLAB --entry 2026-11-09 --price 6250 --qty 8

Without --price the entry fills at that date's open; without --qty the size is
Rs 50,000 of exposure. Daily bars come from Kite, so log in first.
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import kite_data
from src.swing_strategy import compute_mtf_breakeven_pct

LEG_EXPOSURE = 50_000.0
LEVERAGE = 4.4
ARM_EXTRA = 0.02
ATR_N = 14

VARIANTS = {
    "3x flat (committed spec)": (3.0, 3.0, 3.0),
    "CASE 1   3 / 2 / 1.5":     (3.0, 2.0, 1.5),
    "CASE 2   2 / 2 / 1.5":     (2.0, 2.0, 1.5),
    "CASE 3   3 / 1.5 / 1.5":   (3.0, 1.5, 1.5),
    "CASE 4   3 / 2 / 1":       (3.0, 2.0, 1.0),
    "CASES 2+3+4  2 / 1.5 / 1": (2.0, 1.5, 1.0),
    "4x flat (study's pick)":   (4.0, 4.0, 4.0),
}


def atr_wilder(df, n=ATR_N):
    pc = df["Close"].shift(1)
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(),
                    (df["Low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def tier_mult(peak_profit, tiers):
    if peak_profit >= 0.10:
        return tiers[2]
    if peak_profit >= 0.05:
        return tiers[1]
    return tiers[0]


def replay(df, entry_date, avg, qty, tiers):
    """Returns (armed, peak, stop, stopped_on, stop_price)."""
    window = df.loc[df.index >= entry_date]
    value, funded = avg * qty, avg * qty * (1 - 1 / LEVERAGE)
    armed, peak, stop = False, avg, None
    for day, row in window.iterrows():
        held = max(0, (day - entry_date).days)
        if not armed:
            be = compute_mtf_breakeven_pct(value, funded, held)
            if row.High >= avg * (1 + be + ARM_EXTRA):
                armed, peak = True, float(row.High)
                stop = peak - tier_mult(peak / avg - 1, tiers) * float(row.atr)
            continue
        peak = max(peak, float(row.High))
        new_stop = peak - tier_mult(peak / avg - 1, tiers) * float(row.atr)
        stop = new_stop if stop is None else max(stop, new_stop)
        if row.Low <= stop:
            return armed, peak, stop, day, min(float(row.Open), stop)
    return armed, peak, stop, None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("--entry", required=True, help="entry date, YYYY-MM-DD")
    ap.add_argument("--price", type=float, default=None, help="your actual fill price")
    ap.add_argument("--qty", type=int, default=None, help="shares held")
    args = ap.parse_args()

    df = kite_data.fetch_daily_cached(args.symbol, days=400)
    if df is None or df.empty:
        sys.exit(f"no daily bars for {args.symbol}")
    df = df.copy()
    df["atr"] = atr_wilder(df)

    want = pd.Timestamp(args.entry, tz=df.index.tz)
    on_or_after = df.loc[df.index >= want]
    if on_or_after.empty:
        sys.exit(f"{args.entry} is after the last bar ({df.index[-1].date()})")
    entry_date = on_or_after.index[0]
    if entry_date != want:
        print(f"note: {args.entry} was not a trading day -- using {entry_date.date()}")

    avg = args.price if args.price else float(df.at[entry_date, "Open"])
    qty = args.qty if args.qty else max(1, int(LEG_EXPOSURE // avg))
    last = df.index[-1]
    close = float(df.at[last, "Close"])
    atr = float(df.at[last, "atr"])

    print(f"\n{args.symbol}  entered {entry_date.date()} at Rs {avg:,.2f} x {qty} "
          f"= Rs {avg * qty:,.0f} exposure")
    print(f"last bar {last.date()}  close Rs {close:,.2f}  "
          f"({(close / avg - 1) * 100:+.2f}% on the position)  ATR(14) Rs {atr:,.2f}")
    be = compute_mtf_breakeven_pct(avg * qty, avg * qty * (1 - 1 / LEVERAGE),
                                   max(0, (last - entry_date).days))
    print(f"breakeven {be * 100:.2f}%  ->  arms at Rs {avg * (1 + be + ARM_EXTRA):,.2f}\n")

    print(f"{'variant':<28}{'armed':>7}{'peak':>11}{'stop now':>11}{'room':>9}  status")
    print("-" * 82)
    for label, tiers in VARIANTS.items():
        armed, peak, stop, hit, fill = replay(df, entry_date, avg, qty, tiers)
        if not armed:
            print(f"{label:<28}{'no':>7}{'-':>11}{'-':>11}{'-':>9}  not armed yet")
            continue
        if hit is not None:
            print(f"{label:<28}{'yes':>7}{peak:>11,.0f}{stop:>11,.0f}{'-':>9}  "
                  f"STOPPED OUT {hit.date()} at ~Rs {fill:,.0f} "
                  f"({(fill / avg - 1) * 100:+.1f}%, Rs {(fill - avg) * qty:+,.0f})")
        else:
            print(f"{label:<28}{'yes':>7}{peak:>11,.0f}{stop:>11,.0f}"
                  f"{(close / stop - 1) * 100:>8.1f}%  open, "
                  f"locks in Rs {(stop - avg) * qty:+,.0f}")


if __name__ == "__main__":
    main()
