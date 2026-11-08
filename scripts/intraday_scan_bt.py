#!/usr/bin/env python3
"""Backtest the indicator-stack INTRADAY scanner (Screen A) on 15-minute bars.

Implements the scanner spec as given:

  Step 1 -- daily trend gate (same chart filter as the swing screen):
      close > EMA20, EMA20 > EMA50, close > SMA200
      20-day average daily turnover > Rs 10 crore

  Step 2 -- 15-minute entry screen, evaluated on every bar's close:
      RSI(14)  between 55 and 70
      MACD(12,26,9) line above signal and histogram > 0
      ADX(14) > 20 and +DI > -DI
      close > EMA9 and EMA9 > EMA20
      close > session VWAP
      relative volume > 1.5 (bar volume / 20-bar mean)

  Entry:  next 15-minute bar's OPEN. One trade per symbol per day.
  Exit:   arm a trailing stop once unrealised profit >= ARM_EXTRA_PCT + the
          real MIS round-trip breakeven% (src.strategy.compute_intraday_breakeven_pct),
          then trail TRAIL_PCT off the running peak. Hard square-off at 15:15.
          Optional hard stop loss (--stop).

Costs are real Zerodha MIS charges via src.strategy.position_cost.

Intrabar assumption is deliberately pessimistic: inside one 15-minute bar the
peak (high) is taken first and the trailing stop is then tested against the
same bar's low, so a trail exit can fire in the very bar that set the peak.

Run this on the VPS (it needs a live Kite session):

    cd /home/trader/intraday-bot && python3 scripts/intraday_scan_bt.py

Bars are cached under .cache/intraday_scan/ so re-runs with different exit
parameters cost no API calls.
"""

from __future__ import annotations

import argparse
import pickle
import sys
import time as time_module
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import warnings

import numpy as np
import pandas as pd

warnings.simplefilter("ignore", FutureWarning)
warnings.simplefilter("ignore", DeprecationWarning)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import auth
from src.strategy import compute_intraday_breakeven_pct, position_cost
from src.swing_live import NIFTY_50_SYMBOLS

IST = ZoneInfo("Asia/Kolkata")
CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "intraday_scan"

INTERVAL = "15minute"
CHUNK_DAYS = 180          # Kite allows 200 days per request at 15-minute resolution
DAILY_LOOKBACK_DAYS = 1150  # enough daily history for SMA200 before the signal window
REQUEST_DELAY = 0.4

# --- Step 2 thresholds (the spec's intraday screen) ---
RSI_LO, RSI_HI = 55.0, 70.0
ADX_MIN = 20.0
RELVOL_MIN = 1.5
TURNOVER_MIN = 10e7        # Rs 10 crore

ENTRY_FROM = time(9, 30)   # skip the opening bar: VWAP/relvol are meaningless at 09:15
ENTRY_UNTIL = time(14, 0)  # no new entries after this -- needs room before square-off
SQUARE_OFF = time(15, 15)


# ---------------------------------------------------------------- indicators
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi_wilder(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(100.0).where(avg_loss > 0, 100.0)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def adx_di(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14):
    up = high.diff()
    dn = -low.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=high.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    minus_di = 100 * pd.Series(minus_dm, index=high.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    adx = dx.ewm(alpha=1 / n, adjust=False).mean()
    return adx, plus_di, minus_di


def session_vwap(df: pd.DataFrame) -> pd.Series:
    tp = (df["High"] + df["Low"] + df["Close"]) / 3.0
    pv = tp * df["Volume"]
    day = df.index.normalize()
    cum_pv = pv.groupby(day).cumsum()
    cum_v = df["Volume"].groupby(day).cumsum()
    return cum_pv / cum_v.replace(0.0, np.nan)


# ------------------------------------------------------------------- fetching
def _tokens(kite) -> dict[str, int]:
    return {i["tradingsymbol"]: i["instrument_token"] for i in kite.instruments("NSE")}


def _localize(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).rename(
        columns={"date": "datetime", "open": "Open", "high": "High",
                 "low": "Low", "close": "Close", "volume": "Volume"}
    )
    df["datetime"] = pd.to_datetime(df["datetime"])
    if df["datetime"].dt.tz is None:
        df["datetime"] = df["datetime"].dt.tz_localize(IST)
    else:
        df["datetime"] = df["datetime"].dt.tz_convert(IST)
    df = df.set_index("datetime").sort_index()
    df = df[~df.index.duplicated(keep="first")]
    return df[["Open", "High", "Low", "Close", "Volume"]]


def fetch_symbol(kite, tokens: dict[str, int], symbol: str, days: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (15-minute bars, daily bars), both cached on disk."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{symbol}_{days}.pkl"
    if cache.exists():
        with cache.open("rb") as fh:
            return pickle.load(fh)

    token = tokens.get(symbol)
    if token is None:
        print(f"  WARNING: {symbol} not in NSE instrument list")
        return pd.DataFrame(), pd.DataFrame()

    end = datetime.now()
    rows: list[dict] = []
    chunk_start = end - timedelta(days=days)
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS), end)
        try:
            rows.extend(kite.historical_data(token, chunk_start, chunk_end, INTERVAL))
        except Exception as exc:  # noqa: BLE001 -- keep going on one bad window
            print(f"  WARNING: {symbol} {chunk_start.date()}..{chunk_end.date()}: {exc}")
        chunk_start = chunk_end
        time_module.sleep(REQUEST_DELAY)

    daily_rows: list[dict] = []
    try:
        daily_rows = kite.historical_data(
            token, end - timedelta(days=DAILY_LOOKBACK_DAYS), end, "day"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  WARNING: {symbol} daily fetch: {exc}")
    time_module.sleep(REQUEST_DELAY)

    bars, daily = _localize(rows), _localize(daily_rows)
    with cache.open("wb") as fh:
        pickle.dump((bars, daily), fh)
    return bars, daily


# ------------------------------------------------------------------ the screen
def daily_gate(daily: pd.DataFrame) -> pd.Series:
    """Step 1, as a per-date boolean usable on the NEXT session (no look-ahead:
    a bar dated D is only consulted for signals on D+1 and later)."""
    if daily.empty:
        return pd.Series(dtype=bool)
    c = daily["Close"]
    ok = (c > ema(c, 20)) & (ema(c, 20) > ema(c, 50)) & (c > c.rolling(200).mean())
    turnover = (c * daily["Volume"]).rolling(20).mean()
    ok &= turnover > TURNOVER_MIN
    ok.index = daily.index.normalize()
    return ok.fillna(False)


def intraday_signals(bars: pd.DataFrame) -> pd.DataFrame:
    df = bars.copy()
    c = df["Close"]
    df["rsi"] = rsi_wilder(c, 14)
    line, sig, hist = macd(c)
    df["macd_ok"] = (line > sig) & (hist > 0)
    adx, pdi, mdi = adx_di(df["High"], df["Low"], c, 14)
    df["adx"], df["pdi"], df["mdi"] = adx, pdi, mdi
    df["ema9"], df["ema20"] = ema(c, 9), ema(c, 20)
    df["vwap"] = session_vwap(df)
    df["relvol"] = df["Volume"] / df["Volume"].rolling(20).mean()

    df["signal"] = (
        df["rsi"].between(RSI_LO, RSI_HI)
        & df["macd_ok"]
        & (df["adx"] > ADX_MIN)
        & (df["pdi"] > df["mdi"])
        & (c > df["ema9"])
        & (df["ema9"] > df["ema20"])
        & (c > df["vwap"])
        & (df["relvol"] > RELVOL_MIN)
    )
    return df


# ------------------------------------------------------------------ the trades
def run_symbol(symbol, bars, daily, start, end, capital, arm_extra, trail, stop, use_daily_gate):
    df = intraday_signals(bars)
    gate = daily_gate(daily)
    gate_prev = gate.shift(1).astype("boolean").fillna(False).astype(bool) if not gate.empty else gate  # D-1 close decides day D

    trades = []
    for day, chunk in df.groupby(df.index.normalize()):
        if day < start or day > end:
            continue
        if use_daily_gate:
            if gate_prev.empty or day not in gate_prev.index or not bool(gate_prev.loc[day]):
                continue

        times = chunk.index
        entry_i = None
        for i, ts in enumerate(times):
            t = ts.time()
            if t < ENTRY_FROM or t > ENTRY_UNTIL:
                continue
            if bool(chunk["signal"].iloc[i]) and i + 1 < len(times):
                entry_i = i + 1
                break
        if entry_i is None:
            continue

        entry_price = float(chunk["Open"].iloc[entry_i])
        if entry_price <= 0:
            continue
        qty = int(capital // entry_price)
        if qty < 1:
            continue

        value = entry_price * qty
        be_pct = compute_intraday_breakeven_pct("long", value)
        arm_level = entry_price * (1 + be_pct + arm_extra)
        stop_level = entry_price * (1 - stop) if stop else None

        peak = entry_price
        armed = False
        exit_price = None
        reason = ""
        for j in range(entry_i, len(times)):
            hi = float(chunk["High"].iloc[j])
            lo = float(chunk["Low"].iloc[j])
            ts = times[j]

            if stop_level is not None and lo <= stop_level:
                exit_price, reason = stop_level, "stop"
                break

            peak = max(peak, hi)
            if not armed and hi >= arm_level:
                armed = True
            if armed:
                trail_level = peak * (1 - trail)
                if lo <= trail_level:
                    exit_price, reason = max(trail_level, lo), "trail"
                    break
            if ts.time() >= SQUARE_OFF:
                # the 15:15 bar spans 15:15-15:30, so its OPEN is the 15:15 price
                exit_price, reason = float(chunk["Open"].iloc[j]), "squareoff"
                break
        if exit_price is None:
            exit_price, reason = float(chunk["Close"].iloc[-1]), "squareoff"

        gross = (exit_price - entry_price) * qty
        charges = position_cost("long", [(entry_price, qty)], exit_price, qty)
        trades.append(
            dict(symbol=symbol, date=day.date(), entry=entry_price, exit=exit_price,
                 qty=qty, armed=armed, reason=reason, gross=gross,
                 charges=charges, net=gross - charges,
                 pct=(exit_price / entry_price - 1) * 100, be_pct=be_pct * 100)
        )
    return trades


def summarise(label: str, trades: list[dict], capital: float) -> None:
    if not trades:
        print(f"\n{label}: no signals")
        return
    t = pd.DataFrame(trades)
    wins = t[t.net > 0]
    losses = t[t.net <= 0]
    print(f"\n=== {label} ===")
    print(f"trades            {len(t)}")
    print(f"win rate          {len(wins) / len(t) * 100:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"net P&L           Rs {t.net.sum():,.0f}")
    print(f"gross P&L         Rs {t.gross.sum():,.0f}")
    print(f"charges           Rs {t.charges.sum():,.0f}")
    print(f"per trade (net)   Rs {t.net.mean():,.0f}   ({t.net.mean() / capital * 100:+.2f}% of Rs {capital:,.0f})")
    if len(wins):
        print(f"avg win           Rs {wins.net.mean():,.0f}")
    if len(losses):
        print(f"avg loss          Rs {losses.net.mean():,.0f}")
    print(f"best / worst      Rs {t.net.max():,.0f} / Rs {t.net.min():,.0f}")
    print(f"armed (hit arm)   {t.armed.mean() * 100:.0f}%")
    print(f"median breakeven  {t.be_pct.median():.3f}% of position")
    print("exit reasons      " + ", ".join(f"{k}={v}" for k, v in t.reason.value_counts().items()))
    by_year = t.assign(y=pd.to_datetime(t.date).dt.year).groupby("y").net.agg(["count", "sum"])
    print("\nby year:")
    for y, row in by_year.iterrows():
        print(f"  {y}  {int(row['count']):4d} trades   Rs {row['sum']:>12,.0f}")
    top = t.groupby("symbol").net.agg(["count", "sum"]).sort_values("sum")
    print("\nworst 5 symbols:")
    for s, row in top.head(5).iterrows():
        print(f"  {s:<12} {int(row['count']):3d} trades   Rs {row['sum']:>11,.0f}")
    print("best 5 symbols:")
    for s, row in top.tail(5).iloc[::-1].iterrows():
        print(f"  {s:<12} {int(row['count']):3d} trades   Rs {row['sum']:>11,.0f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=760, help="calendar days of 15m history to fetch")
    ap.add_argument("--years", type=float, default=2.0, help="signal window, years back from today")
    ap.add_argument("--capital", type=float, default=50_000, help="rupees deployed per trade")
    ap.add_argument("--arm", type=float, default=0.5, help="arm trailing at breakeven%% + this %%")
    ap.add_argument("--trail", type=float, default=0.3, help="trail %% off the peak once armed")
    ap.add_argument("--stop", type=float, default=0.0, help="hard stop loss %% (0 = none)")
    ap.add_argument("--no-daily-gate", action="store_true", help="drop the Step 1 daily trend filter")
    ap.add_argument("--symbols", default="", help="comma-separated override of the universe")
    ap.add_argument("--dump", default="", help="write every trade to this CSV")
    args = ap.parse_args()

    symbols = (
        [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        if args.symbols else sorted(NIFTY_50_SYMBOLS)
    )

    kite = auth.get_kite()
    tokens = _tokens(kite)

    end = pd.Timestamp.now(tz=IST).normalize()
    start = end - pd.Timedelta(int(args.years * 365), unit="D")

    all_trades: list[dict] = []
    for n, sym in enumerate(symbols, 1):
        bars, daily = fetch_symbol(kite, tokens, sym, args.days)
        if bars.empty:
            print(f"[{n}/{len(symbols)}] {sym}: no bars", flush=True)
            continue
        trades = run_symbol(
            sym, bars, daily, start, end, args.capital,
            args.arm / 100.0, args.trail / 100.0, args.stop / 100.0,
            not args.no_daily_gate,
        )
        all_trades.extend(trades)
        print(f"[{n}/{len(symbols)}] {sym}: {len(bars):>6} bars, {len(trades):>3} trades", flush=True)

    label = (f"INTRADAY scan  arm=breakeven+{args.arm}%  trail={args.trail}%  "
             f"stop={args.stop or 'none'}  daily-gate={'off' if args.no_daily_gate else 'on'}  "
             f"{start.date()}..{end.date()}")
    summarise(label, all_trades, args.capital)

    if args.dump and all_trades:
        pd.DataFrame(all_trades).to_csv(args.dump, index=False)
        print(f"\nwrote {args.dump}")


if __name__ == "__main__":
    main()
