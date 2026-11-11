#!/usr/bin/env python3
"""Tiered-ATR trailing-stop study for the committed 52-week-low MTF strategy.

Answers one question: does tightening the trailing stop as a position gets
further into profit book more money than the flat 3x ATR(14) the strategy
spec (STRATEGY_MTF_52W_LOW.md) currently uses?

Everything except the trail distance is held exactly at that spec -- Nifty 100,
entry on a fresh 252-session low with a 20-day (High-Low)/Close >= 2.5% filter,
buy at the next open, Rs 50,000 exposure per leg at 4.4x MTF, at most ONE
averaging leg 3% below the entry price, arm when the high reaches
avg x (1 + breakeven% + 2%) with breakeven recomputed daily including accrued
MTF interest, then trail off the running peak high. No stop loss, no target.

A tier is chosen from the position's PEAK profit, not its current profit, and
the stop level is ratcheted, so the stop can only ever move up. Tiering off
current profit would let the stop widen again on a pullback, which is not a
trailing stop.

Usage:
    python3 scripts/run_atr_tier_backtest.py [start_date] [--book-half-at PCT]

    start_date   ISO date the signal window opens (default 2022-06-01)
    --book-half-at
                 also sell half the position the first time it trades this many
                 percent above its average, and trail the rest (default: off)

Daily bars come from Kite, so today's login must be done first.
"""

import argparse
import csv
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import kite_data
from src.swing_strategy import (
    MTF_PLEDGE_CHARGE, compute_mtf_breakeven_pct, mtf_interest, mtf_order_cost,
)

NIFTY100_CSV = Path(__file__).resolve().parent.parent / "data" / "nifty100.csv"
HISTORY_DAYS = 1990  # Kite rejects a "day" request spanning more than 2000 days

LEG_EXPOSURE = 50_000.0
LEVERAGE = 4.4
OWN_PER_LEG = LEG_EXPOSURE / LEVERAGE
FUNDED_PER_LEG = LEG_EXPOSURE - OWN_PER_LEG
WALLET = 700_000.0
LOOKBACK = 252
VOL_FILTER = 0.025
MAX_AVERAGES = 1
AVG_DROP = 0.03
ARM_EXTRA = 0.02
ATR_N = 14


def atr_wilder(df, n=ATR_N):
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def prepare(data):
    out = {}
    for sym, df in data.items():
        if len(df) < LOOKBACK + ATR_N + 10:
            continue
        d = df.copy()
        d["atr"] = atr_wilder(d)
        d["low252"] = d["Low"].rolling(LOOKBACK).min().shift(1)   # prior sessions only
        d["vol20"] = ((d["High"] - d["Low"]) / d["Close"]).rolling(20).mean()
        d["signal"] = (d["Low"] <= d["low252"]) & (d["vol20"] >= VOL_FILTER)
        out[sym] = d
    return out


def tier_mult(peak_profit, tiers):
    """tiers = (m_below5, m_5to10, m_above10)."""
    if peak_profit >= 0.10:
        return tiers[2]
    if peak_profit >= 0.05:
        return tiers[1]
    return tiers[0]


def run(prepped, tiers, start=None, end=None, book_half_at=None):
    dates = sorted({d for df in prepped.values() for d in df.index})
    if start is not None:
        dates = [d for d in dates if d >= start]
    if end is not None:
        dates = [d for d in dates if d <= end]

    open_pos = {}
    trades = []
    blocked = 0
    own_used = 0.0
    own_series = []
    pending = []  # (symbol,) signalled yesterday -> buy at today's open

    for day in dates:
        # --- fills from yesterday's signals, at today's open ---
        for sym in pending:
            df = prepped[sym]
            if day not in df.index or sym in open_pos:
                continue
            px = float(df.at[day, "Open"])
            if px <= 0:
                continue
            if own_used + OWN_PER_LEG > WALLET:
                blocked += 1
                continue
            qty = int(LEG_EXPOSURE // px)
            if qty < 1:
                continue
            own_used += OWN_PER_LEG
            open_pos[sym] = dict(
                legs=[(px, qty, day)], entry_px=px, entry_date=day,
                armed=False, peak=px, stop=None, avgs=0,
            )
        pending = []

        # --- manage open positions on today's bar ---
        for sym in list(open_pos):
            df = prepped[sym]
            if day not in df.index:
                continue
            row = df.loc[day]
            o, h, l, c = float(row.Open), float(row.High), float(row.Low), float(row.Close)
            atr = float(row.atr)
            pos = open_pos[sym]
            qty = sum(q for _, q, _ in pos["legs"])
            value = sum(p * q for p, q, _ in pos["legs"])
            avg = value / qty
            funded = FUNDED_PER_LEG * len(pos["legs"])
            held = max(0, (day - pos["legs"][0][2]).days)

            if pos["armed"] and book_half_at is not None and not pos.get("booked"):
                tgt = avg * (1 + book_half_at)
                if h >= tgt:
                    half = qty // 2
                    if half >= 1:
                        fill = max(o, tgt)  # a gap-up fills at the open, not below it
                        part_funded = funded * half / qty
                        trades.append(close_partial(sym, pos, fill, day, avg, half,
                                                    part_funded, held, "book_half"))
                        # shrink every leg pro-rata so avg price is unchanged
                        keep = qty - half
                        scale = keep / qty
                        pos["legs"] = [(px_, max(1, int(q_ * scale)), d_) for px_, q_, d_ in pos["legs"]]
                        pos["booked"] = True
                        qty = sum(q for _, q, _ in pos["legs"])
                        value = sum(px_ * q for px_, q, _ in pos["legs"])
                        avg = value / qty
                        funded = FUNDED_PER_LEG * len(pos["legs"]) * scale

            if pos["armed"]:
                pos["peak"] = max(pos["peak"], h)
                peak_profit = pos["peak"] / avg - 1.0
                m = tier_mult(peak_profit, tiers)
                new_stop = pos["peak"] - m * atr
                # ratchet: a trailing stop never steps back down
                pos["stop"] = new_stop if pos["stop"] is None else max(pos["stop"], new_stop)
                if l <= pos["stop"]:
                    fill = min(o, pos["stop"])  # a gap-down fills at the open
                    trades.append(close(sym, pos, fill, day, avg, qty, value, funded, held, "trail"))
                    del open_pos[sym]
                    own_used -= OWN_PER_LEG * len(pos["legs"])
                    continue
            else:
                be = compute_mtf_breakeven_pct(value, funded, held)
                if h >= avg * (1 + be + ARM_EXTRA):
                    pos["armed"] = True
                    pos["peak"] = h
                    peak_profit = pos["peak"] / avg - 1.0
                    pos["stop"] = pos["peak"] - tier_mult(peak_profit, tiers) * atr
                elif pos["avgs"] < MAX_AVERAGES and l <= pos["entry_px"] * (1 - AVG_DROP):
                    add_px = min(o, pos["entry_px"] * (1 - AVG_DROP))
                    if own_used + OWN_PER_LEG <= WALLET:
                        aq = int(LEG_EXPOSURE // add_px)
                        if aq >= 1:
                            pos["legs"].append((add_px, aq, day))
                            pos["avgs"] += 1
                            own_used += OWN_PER_LEG
                    else:
                        blocked += 1

        # --- today's signals fill tomorrow ---
        for sym, df in prepped.items():
            if sym in open_pos or day not in df.index:
                continue
            if bool(df.at[day, "signal"]):
                pending.append(sym)

        own_series.append(own_used)

    # mark still-open positions to the last close
    open_rows = []
    for sym, pos in open_pos.items():
        df = prepped[sym]
        last = df.index[-1]
        c = float(df.at[last, "Close"])
        qty = sum(q for _, q, _ in pos["legs"])
        value = sum(p * q for p, q, _ in pos["legs"])
        avg = value / qty
        funded = FUNDED_PER_LEG * len(pos["legs"])
        held = max(0, (last - pos["legs"][0][2]).days)
        open_rows.append(close(sym, pos, c, last, avg, qty, value, funded, held, "open"))

    return pd.DataFrame(trades), pd.DataFrame(open_rows), blocked, own_series


def close_partial(sym, pos, px, day, avg, qty, funded, held, reason):
    """Book `qty` shares out of an open position. Charges the sell side and the
    interest accrued on this slice only; the pledge/unpledge pair and the buy-side
    charges stay with the remainder, which is still open."""
    gross = (px - avg) * qty
    costs = mtf_order_cost(px * qty, "sell") + mtf_interest(funded, held)
    return dict(symbol=sym, entry_date=pos["legs"][0][2], exit_date=day, avg=avg,
                exit=px, qty=qty, legs=len(pos["legs"]), held=held,
                gross=gross, costs=costs, net=gross - costs,
                pct=(px / avg - 1) * 100, reason=reason,
                peak_pct=(pos["peak"] / avg - 1) * 100, armed=True)


def close(sym, pos, px, day, avg, qty, value, funded, held, reason):
    gross = (px - avg) * qty
    buy_costs = sum(mtf_order_cost(p * q, "buy") for p, q, _ in pos["legs"])
    sell_cost = mtf_order_cost(px * qty, "sell")
    pledge = MTF_PLEDGE_CHARGE * (len(pos["legs"]) + 1)
    interest = mtf_interest(funded, held)
    costs = buy_costs + sell_cost + pledge + interest
    return dict(symbol=sym, entry_date=pos["legs"][0][2], exit_date=day, avg=avg,
                exit=px, qty=qty, legs=len(pos["legs"]), held=held,
                gross=gross, costs=costs, net=gross - costs,
                pct=(px / avg - 1) * 100, reason=reason,
                peak_pct=(pos["peak"] / avg - 1) * 100 if pos["armed"] else np.nan,
                armed=pos["armed"])


VARIANTS = {
    "baseline  3x flat (committed spec)": (3.0, 3.0, 3.0),
    "CASE 1    3x / 2x / 1.5x":           (3.0, 2.0, 1.5),
    "case 2    2x / 2x / 1.5x":           (2.0, 2.0, 1.5),
    "case 3    3x / 1.5x / 1.5x":         (3.0, 1.5, 1.5),
    "case 4    3x / 2x / 1x":             (3.0, 2.0, 1.0),
    "CASES 2+3+4  2x / 1.5x / 1x":        (2.0, 1.5, 1.0),
    "control   2x flat":                  (2.0, 2.0, 2.0),
    "control   1.5x flat":                (1.5, 1.5, 1.5),
    # Loosening, the opposite of what the tiered cases do. Included because it is
    # what the study actually found: every tightening costs money, and widening
    # the trail keeps paying right out to the point where it stops being a stop.
    "LOOSER    4x flat":                  (4.0, 4.0, 4.0),
    "LOOSER    5x flat":                  (5.0, 5.0, 5.0),
    "LOOSER    6x flat":                  (6.0, 6.0, 6.0),
    "WIDENING  4x / 5x / 6x":             (4.0, 5.0, 6.0),
}


def load_universe() -> list[str]:
    with open(NIFTY100_CSV) as fh:
        return [row["Symbol"] for row in csv.DictReader(fh)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("start", nargs="?", default="2022-06-01",
                    help="ISO date the signal window opens")
    ap.add_argument("--book-half-at", type=float, default=None,
                    help="also sell half the position this many %% above average")
    args = ap.parse_args()

    symbols = load_universe()
    print(f"Fetching {HISTORY_DAYS}d of daily bars for {len(symbols)} Nifty 100 symbols ...")
    data = {}
    for i, sym in enumerate(symbols, 1):
        df = kite_data.fetch_daily_cached(sym, days=HISTORY_DAYS)
        if df is not None and not df.empty:
            data[sym] = df
        if i % 20 == 0:
            print(f"  {i}/{len(symbols)}", flush=True)

    prepped = prepare(data)
    print(f"{len(prepped)} symbols with enough history, "
          f"{min(d.index.min() for d in prepped.values()).date()} .. "
          f"{max(d.index.max() for d in prepped.values()).date()}")

    # A fixed start date, NOT max(index.min()): one late listing (ENRIN, 2025-06)
    # would otherwise truncate the whole study to a few months. Symbols without
    # 252 bars yet have a NaN low252 and cannot signal, so they exclude themselves.
    first = pd.Timestamp(args.start, tz="Asia/Kolkata")
    book = args.book_half_at / 100.0 if args.book_half_at else None

    print(f"\nsignal window opens {first.date()}"
          + (f", booking half at +{args.book_half_at}%" if book else ""))
    print(f"\n{'variant':<36} {'trades':>6} {'win':>6} {'closed':>11} {'open':>10} "
          f"{'TOTAL':>11} {'/trade':>8} {'hold':>6}")
    print("-" * 104)
    for label, tiers in VARIANTS.items():
        closed, still_open, blocked, own = run(prepped, tiers, start=first, book_half_at=book)
        full = closed[closed.reason != "book_half"] if len(closed) else closed
        net = closed.net.sum()
        unreal = still_open.net.sum() if len(still_open) else 0.0
        win = (full.net > 0).mean() * 100 if len(full) else 0.0
        print(f"{label:<36} {len(full):>6} {win:>5.1f}% {net:>11,.0f} {unreal:>10,.0f} "
              f"{net + unreal:>11,.0f} {net / max(1, len(full)):>8,.0f} "
              f"{full.held.median() if len(full) else 0:>5.0f}d")
        if blocked:
            print(f"{'':<36} ({blocked} entries blocked by the Rs {WALLET:,.0f} wallet, "
                  f"peak own capital Rs {max(own):,.0f})")


if __name__ == "__main__":
    main()
