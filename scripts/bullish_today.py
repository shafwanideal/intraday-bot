"""Today's technically strongest Nifty 50 stocks, from Kite daily bars.

WHAT THIS IS: a transparent ranking of where each Nifty 50 stock sits on
eight standard technical measures, so you can see the board at a glance and
why each name scores as it does.

WHAT THIS IS NOT: a prediction. Stacked bullish filters were tested against
this very universe earlier and did not pick winners out of sample -- 13
filters, 2 survived three windows and both failed the fourth; all 8 gates
together LOWERED the win rate; volatility ranking went from +0.46 to +0.10
correlation out of sample; top-N by past performance inverted. Treat the
score as a description of the chart, not a forecast of the next move.

NO FUNDAMENTALS. Kite Connect serves price, volume, OHLC, instruments,
orders, positions, holdings and margins -- there is no P/E, earnings,
revenue, ROE or debt in the API at all. Any fundamental screen needs a
different source; this script does not invent one.

    .venv/bin/python3 scripts/bullish_today.py
    .venv/bin/python3 scripts/bullish_today.py --all     # every stock, not just the top
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import kite_data, screener  # noqa: E402

DAYS = 500          # enough for a 200-DMA and a 52-week range
BENCH = "NIFTY 50"  # relative strength is measured against the index itself


def rsi(series: pd.Series, period: int = 14) -> float:
    d = series.diff()
    gain = d.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, pd.NA)
    return float((100 - 100 / (1 + rs)).iloc[-1])


def atr_pct(df: pd.DataFrame, period: int = 14) -> float:
    h, l, c = df["High"], df["Low"], df["Close"].shift(1)
    tr = pd.concat([h - l, (h - c).abs(), (l - c).abs()], axis=1).max(axis=1)
    return float(tr.ewm(alpha=1 / period, adjust=False).mean().iloc[-1] / df["Close"].iloc[-1] * 100)


def metrics(df: pd.DataFrame, bench_ret: float | None) -> dict | None:
    if len(df) < 210:
        return None
    c = df["Close"]
    last = float(c.iloc[-1])
    ma20, ma50, ma200 = (float(c.rolling(n).mean().iloc[-1]) for n in (20, 50, 200))
    hi52, lo52 = float(df["High"].tail(250).max()), float(df["Low"].tail(250).min())
    vol20 = float(df["Volume"].tail(20).mean())
    up = 0
    for i in range(len(c) - 1, 0, -1):
        if c.iloc[i] > c.iloc[i - 1]:
            up += 1
        else:
            break
    ret20 = (last / float(c.iloc[-21]) - 1) * 100
    return dict(
        price=last,
        from_high=(last / hi52 - 1) * 100,
        above_low=(last / lo52 - 1) * 100,
        vs_ma20=(last / ma20 - 1) * 100,
        vs_ma50=(last / ma50 - 1) * 100,
        vs_ma200=(last / ma200 - 1) * 100,
        stacked=ma20 > ma50 > ma200,
        rsi=rsi(c),
        atrp=atr_pct(df),
        vol_x=float(df["Volume"].iloc[-1]) / vol20 if vol20 else 0.0,
        ret20=ret20,
        rs=ret20 - bench_ret if bench_ret is not None else float("nan"),
        up_days=up,
    )


def score(m: dict) -> tuple[int, list[str]]:
    """Eight checks, one point each. Shown individually so nothing is hidden
    inside a single number."""
    hits = []
    if m["stacked"]:
        hits.append("MA stack 20>50>200")
    if m["vs_ma200"] > 0:
        hits.append("above 200-DMA")
    if m["vs_ma20"] > 0:
        hits.append("above 20-DMA")
    if m["from_high"] > -10:
        hits.append("within 10% of 52w high")
    if 55 <= m["rsi"] <= 70:
        hits.append(f"RSI {m['rsi']:.0f} strong, not overbought")
    if m["vol_x"] >= 1.2:
        hits.append(f"volume {m['vol_x']:.1f}x avg")
    if m["rs"] == m["rs"] and m["rs"] > 0:
        hits.append(f"beating NIFTY by {m['rs']:.1f}pp over 20d")
    if m["ret20"] > 0:
        hits.append(f"20d return {m['ret20']:+.1f}%")
    return len(hits), hits


def main() -> None:
    show_all = "--all" in sys.argv
    syms = screener.load_nifty50_symbols()
    print(f"Fetching {len(syms)} Nifty 50 symbols + {BENCH} ...", flush=True)
    data = kite_data.fetch_daily_many(syms + [BENCH], days=DAYS)

    bench_ret = None
    if BENCH in data and len(data[BENCH]) > 21:
        b = data[BENCH]["Close"]
        bench_ret = (float(b.iloc[-1]) / float(b.iloc[-21]) - 1) * 100

    rows = []
    for s in syms:
        df = data.get(s)
        if df is None or df.empty:
            continue
        m = metrics(df, bench_ret)
        if m:
            m["symbol"] = s
            m["score"], m["hits"] = score(m)
            m["asof"] = df.index[-1].date()
            rows.append(m)
    if not rows:
        print("No data. Is today's Kite login done?")
        return

    rows.sort(key=lambda r: (-r["score"], -r["rs"] if r["rs"] == r["rs"] else 0))
    asof = rows[0]["asof"]
    print(f"\nAs of {asof}"
          + (f" | NIFTY 50 20-day return {bench_ret:+.2f}%" if bench_ret is not None else ""))
    print("=" * 118)
    print(f"{'#':<4}{'stock':<13}{'score':>6}{'price':>10}{'vs 52wH':>9}{'vs 20d':>8}"
          f"{'vs 50d':>8}{'vs 200d':>9}{'RSI':>6}{'ATR%':>7}{'vol x':>7}"
          f"{'20d ret':>9}{'vs NIFTY':>10}{'up':>4}")
    print("-" * 118)
    for i, r in enumerate(rows if show_all else rows[:15], 1):
        print(f"{i:<4}{r['symbol']:<13}{r['score']:>4}/8{r['price']:>10,.1f}"
              f"{r['from_high']:>8.1f}%{r['vs_ma20']:>7.1f}%{r['vs_ma50']:>7.1f}%"
              f"{r['vs_ma200']:>8.1f}%{r['rsi']:>6.0f}{r['atrp']:>6.1f}%{r['vol_x']:>7.2f}"
              f"{r['ret20']:>8.1f}%{r['rs']:>9.1f}{r['up_days']:>4}")

    print()
    print("=" * 118)
    print("  TOP 3 -- why each one scores")
    print("=" * 118)
    for r in rows[:3]:
        print(f"\n  {r['symbol']}  ({r['score']}/8)  Rs {r['price']:,.1f}")
        for h in r["hits"]:
            print(f"     + {h}")
        misses = [x for x in [
            "MA stack 20>50>200" if not r["stacked"] else None,
            "below 200-DMA" if r["vs_ma200"] <= 0 else None,
            "below 20-DMA" if r["vs_ma20"] <= 0 else None,
            f"{r['from_high']:.0f}% off its 52w high" if r["from_high"] <= -10 else None,
            f"RSI {r['rsi']:.0f}" if not (55 <= r["rsi"] <= 70) else None,
            f"volume only {r['vol_x']:.1f}x avg" if r["vol_x"] < 1.2 else None,
            f"lagging NIFTY by {abs(r['rs']):.1f}pp" if r["rs"] == r["rs"] and r["rs"] <= 0 else None,
            f"20d return {r['ret20']:+.1f}%" if r["ret20"] <= 0 else None,
        ] if x]
        for x in misses:
            print(f"     - {x}")

    print()
    print("  A high score means the chart looks strong today. It is not a forecast:")
    print("  stacked bullish filters did not predict returns out of sample on this")
    print("  universe. Use it to see the board, then decide for yourself.")


if __name__ == "__main__":
    main()
