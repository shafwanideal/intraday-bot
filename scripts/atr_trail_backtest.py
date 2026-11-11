"""Backtest: does an ATR-based, profit-tiered trailing stop beat the swing
guardian's current flat 1% trail? Requested 2026-11-11 -- the trader proposed
trailing by a multiple of the stock's own ATR (Average True Range) instead of
a flat percentage, tightening the multiple as profit grows (a "Chandelier
Exit" style tiered trail), reasoning that an extended move carries more
reversal risk and deserves a tighter leash.

Three configurations compared, same entry rule, same costs, same arm
threshold (PROFIT_TARGET_PCT, flat 2% -- this script does not model
MTF-funding-day breakeven, same simplification run_swing_backtest.py already
makes; only the TRAILING distance after arming differs between configs):
  - baseline : flat TRAILING_PCT (1%), today's live behavior
  - set_a    : tiered ATR, trader's numbers   -- 2x <5% profit, 1.5x 5-10%, 1x beyond
  - set_b    : tiered ATR, counter-proposal   -- 2.5x <5%, 1.75x 5-10%, 1.25x beyond

All three preserve the SAME floor-lock swing_live.py uses live: once armed,
the stop can only ever rise toward the peak, never retreat below the price
that triggered arming (SwingEngine.update() now enforces this for every
trailing mode, pct or ATR -- see swing_strategy.py).

Entry rule, chosen to isolate the EXIT mechanism as the only variable: enter
on the first day with enough history for a 14-day ATR, then whenever a
position closes, re-enter the very next trading day at that day's close.
This is NOT a claim about when to actually buy -- it exists purely so every
configuration sees the exact same sequence of entries and only the trailing
logic differs, which is the only question this backtest answers.

ATR(14) is computed once at entry (true range = max(high-low,
|high-prev_close|, |low-prev_close|), simple 14-day rolling mean) and held
fixed for that trade's duration, matching SwingPosition.atr's existing
"this symbol's ATR at entry" semantics -- it is not recalculated daily while
a position is open.

Universe: the trader's actual current MTF holdings (BAJAJFINSV, HAL, INFY,
ITC, LUPIN, TATAPOWER, TORNTPHARM, YATHARTH), ~2.25 years of free daily bars
from Yahoo Finance (src.yahoo_daily) -- no live Kite session is needed to run
this.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from src import swing_strategy, yahoo_daily  # noqa: E402

SYMBOLS = ["BAJAJFINSV", "HAL", "INFY", "ITC", "LUPIN", "TATAPOWER", "TORNTPHARM", "YATHARTH"]
ATR_WINDOW = 14
HISTORY_DAYS = 850

CONFIGS = {
    "baseline (flat 1%)": {"trailing_pct": 0.01},
    "flat 0.5%": {"trailing_pct": 0.005},
    "set_a (2x/1.5x/1x ATR)": {"atr_tiers": [(0.05, 2.0), (0.10, 1.5), (float("inf"), 1.0)]},
    "set_b (2.5x/1.75x/1.25x ATR)": {"atr_tiers": [(0.05, 2.5), (0.10, 1.75), (float("inf"), 1.25)]},
}


def _atr_series(df: pd.DataFrame) -> pd.Series:
    prev_close = df["Close"].shift(1)
    tr = pd.concat(
        [df["High"] - df["Low"], (df["High"] - prev_close).abs(), (df["Low"] - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(ATR_WINDOW).mean()


def run_one(symbol: str, df: pd.DataFrame, config: dict) -> dict:
    atr = _atr_series(df)
    engine = swing_strategy.SwingEngine(stop_fill_at_level=True, **config)
    dates = df.index
    i = ATR_WINDOW  # first index with a valid ATR
    while i < len(dates):
        d = dates[i]
        row = df.loc[d]
        if symbol not in engine.open_positions:
            a = atr.loc[d]
            if pd.notna(a) and a > 0:
                engine.enter(symbol, float(row["Close"]), d, atr=float(a))
        else:
            engine.update(symbol, float(row["Close"]), d, low=float(row["Low"]), open_=float(row["Open"]))
        i += 1
    return {"trades": engine.closed_trades, "still_open": engine.open_positions, "last_close": float(df["Close"].iloc[-1])}


def summarize(label: str, result: dict) -> dict:
    trades = result["trades"]
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    total_pnl = sum(t["pnl"] for t in trades)
    quick_stops = sum(1 for t in trades if t["reason"] == "trailing_stop_exit" and (t["exit_date"] - t["entry_date"]).days <= 2)
    worst_trade = min((t["pnl"] for t in trades), default=0.0)
    return {
        "config": label,
        "trades": n,
        "win_rate": round(100 * len(wins) / n, 1) if n else 0.0,
        "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 0) if wins else 0.0,
        "quick_stops_2d": quick_stops,
        "total_pnl": round(total_pnl, 0),
        "worst_trade_pnl": round(worst_trade, 0),
    }


def main() -> None:
    print(f"Fetching {HISTORY_DAYS}d of daily bars for {len(SYMBOLS)} symbols ...")
    daily_data = yahoo_daily.fetch_daily_many(SYMBOLS, days=HISTORY_DAYS)

    all_rows = []
    per_symbol_rows = []
    for config_label, config in CONFIGS.items():
        agg_trades = []
        for symbol in SYMBOLS:
            df = daily_data.get(symbol)
            if df is None or len(df) < ATR_WINDOW + 5:
                print(f"  skipping {symbol}: insufficient data")
                continue
            result = run_one(symbol, df, config)
            agg_trades.extend(result["trades"])
            row = summarize(symbol, result)
            row["symbol"] = symbol
            row["config"] = config_label
            per_symbol_rows.append(row)
        summary = summarize(config_label, {"trades": agg_trades})
        all_rows.append(summary)

    print("\n=== AGGREGATE, all 8 symbols combined ===")
    agg_df = pd.DataFrame(all_rows)[["config", "trades", "win_rate", "avg_win", "quick_stops_2d", "total_pnl", "worst_trade_pnl"]]
    print(agg_df.to_string(index=False))

    print("\n=== PER-SYMBOL BREAKDOWN ===")
    for config_label in CONFIGS:
        print(f"\n-- {config_label} --")
        sub = [r for r in per_symbol_rows if r["config"] == config_label]
        sub_df = pd.DataFrame(sub)[["symbol", "trades", "win_rate", "avg_win", "quick_stops_2d", "total_pnl", "worst_trade_pnl"]]
        print(sub_df.to_string(index=False))


if __name__ == "__main__":
    main()
