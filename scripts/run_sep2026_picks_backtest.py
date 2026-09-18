"""Backtest for Shafwan's own dated stock picks, 2026-09-07 through 2026-09-18,
as given directly ("do back test for the following ... do it for these dates").

Data source is Yahoo (login-free) rather than Kite: this environment has no
Kite session/token, and Yahoo's chart API needs neither. See
`src.yahoo_intraday`'s own module docstring for the tradeoffs that implies
(different vendor's prices than the bot actually trades on, 5-min history
only goes back ~60 days, last bar or two of a live session can be missing).

Direction defaults to "long" for any symbol the user didn't explicitly mark
"short" -- matching STRATEGY.md's stated convention ("every stock is traded
long by default; a stock is only shorted if explicitly flagged short").

Two picks could not be matched to a real, currently-listed NSE ticker via
Yahoo and are EXCLUDED, not guessed:
- "GE Vernova" (8 Sep) -- GE Vernova trades on the NYSE (GEV); no separate
  NSE listing was found under any tried ticker, and Yahoo's search endpoint
  returned nothing for it either.
Everything else was verified against Yahoo's own listed company name before
being included here (see the mapping below) -- most notably "grow" ->
GROWW.NS (Billionbrains Garage Ventures Ltd, Groww's listed parent) and
"skyway airways" -> SKYWAYS.NS (Skyways Air Services Ltd), which are not
obvious ticker spellings.

Sizing: this uses run_backtest's plain equal-split mode (no per-symbol pct),
with total_units = max_concurrent_positions = 6 -- the largest single day's
symbol count here -- so every day's position size is 1/6 of margin_capital
(leveraged), even on a day with only 3 picks. This does NOT reproduce
live.py's real two-tranche sizing (half of capital to whatever's given
pre-market, split among just THAT day's count) since run_backtest has one
fixed total_units for the whole multi-day run, not a per-day one -- a real
day with 3 picks would size each position roughly twice as large live as it
does here. Flagged rather than silently misrepresented.
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import backtest, yahoo_intraday
from src.strategy import compute_daily_loss_cap, compute_portfolio_profit_lock_trigger

# symbol -> (yahoo/NSE ticker, resolved company name, as confirmed via Yahoo's own meta)
# kept here only as a paper trail for the mapping below; not read by the code.
RESOLVED = {
    "wipro": "WIPRO",
    "mazadock": "MAZDOCK",  # Mazagon Dock Shipbuilders
    "mazagondoc": "MAZDOCK",
    "indigo": "INDIGO",  # InterGlobe Aviation
    "interglobe aviation": "INDIGO",
    "fortis": "FORTIS",
    "npst": "NPST",
    "ongc": "ONGC",
    "bel": "BEL",
    "dr reddys": "DRREDDY",
    "hdfc": "HDFCBANK",  # no standalone "HDFC" ticker since the 2023 merger
    "skyway airways": "SKYWAYS",  # Skyways Air Services Ltd
    "gpt infra": "GPTINFRA",
    "infosys": "INFY",
    "tata chemicals": "TATACHEM",
    "kec": "KEC",
    "grow": "GROWW",  # Billionbrains Garage Ventures Ltd
    "spectrum electrical industries": "SPECTRUM",
    "shankesh jewellers": "SHANKESH",
    "hindustan copper": "HINDCOPPER",
    "neogen": "NEOGEN",
    "granules": "GRANULES",
    "dilip builtcon": "DBL",
    "shakti pump": "SHAKTIPUMP",
    "redington": "REDINGTON",
    "jindal steel": "JINDALSTEL",
    "coforge": "COFORGE",
    "adani ent": "ADANIENT",
    "pay tm": "PAYTM",
    "hal": "HAL",
    "pvr": "PVRINOX",  # merged PVR-Inox
    "rvnl": "RVNL",
    "molbio diag": "MOLBIO",
}
EXCLUDED = ["ge vernova (8 Sep) -- no NSE listing found"]

DAILY_PLAN: dict[date, dict[str, str]] = {
    date(2026, 9, 7): {"RVNL": "long", "MAZDOCK": "long", "MOLBIO": "long"},
    date(2026, 9, 8): {"HAL": "long", "PVRINOX": "long"},  # GE Vernova excluded, see module docstring
    date(2026, 9, 9): {"COFORGE": "short", "ADANIENT": "long", "PAYTM": "long"},
    date(2026, 9, 10): {"DBL": "long", "SHAKTIPUMP": "long", "REDINGTON": "long", "JINDALSTEL": "long"},
    date(2026, 9, 11): {
        "INDIGO": "short",
        "SPECTRUM": "long",
        "SHANKESH": "long",
        "HINDCOPPER": "short",
        "NEOGEN": "short",
        "GRANULES": "short",
    },
    date(2026, 9, 15): {"INFY": "long", "HDFCBANK": "long", "TATACHEM": "long", "KEC": "long", "GROWW": "long"},
    date(2026, 9, 17): {
        "WIPRO": "short",
        "MAZDOCK": "long",
        "INDIGO": "long",
        "FORTIS": "long",
        "NPST": "long",
        "ONGC": "short",
    },
    date(2026, 9, 18): {
        "INDIGO": "long",
        "BEL": "long",
        "DRREDDY": "long",
        "HDFCBANK": "long",
        "SKYWAYS": "long",
        "GPTINFRA": "long",
    },
}

GRID_PCT = 0.015  # current standing default, strategy.GRID_PCT
GOAL_DAILY_PNL = 2_000
MARGIN_CAPITAL = 50_000  # placeholder -- backtest.MARGIN_CAPITAL's standing default. Substitute your
# real day's margin capital for an accurate number; P&L scales roughly linearly with it.
TOTAL_UNITS = 6  # largest single day's symbol count (11 Sep, 17 Sep, 18 Sep) -- see module docstring
MAX_CONCURRENT_POSITIONS = 6

ALL_SYMBOLS = sorted({sym for day_plan in DAILY_PLAN.values() for sym in day_plan})

CAVEATS = """
CAVEATS (read before acting on these numbers):
- Data: Yahoo Finance 5-minute bars (login-free), NOT Kite -- this environment has no
  Kite session. Expect small price differences from the feed the bot actually trades on.
- Direction defaults to "long" for every pick the request didn't mark "short" --
  matching STRATEGY.md's stated convention.
- "GE Vernova" (8 Sep) is EXCLUDED -- no NSE listing found for it, see module docstring.
  That day's simulated basket is HAL + PVRINOX only, not the 3 originally named.
- Sizing is an equal 1/6 split of margin_capital (leveraged) on every day, sized for the
  BUSIEST day (6 symbols) -- see module docstring for why this understates position size
  on lighter days relative to what live.py's real two-tranche sizing would actually give
  a 2-4 symbol day.
- Trailing stop: arms at grid_pct (1.5%) favorable move, trails 0.75% behind the best
  price seen since (trail_pct = grid_pct / 2, the current live/shadow default) -- no ATR
  scaling, matching the 2026-09-13 rewrite.
- No averaging leg (enable_averaging=False) -- matches the current live default.
- Daily loss cap = 4% of margin_capital; portfolio profit lock arms at 2.667% of
  margin_capital and then holds a FIXED floor there (no ratchet) -- both computed per the
  2026-09-13 layered-trailing-stop/portfolio-floor spec, same as live.py/shadow.py.
- Entries are at each day's real 9:15 AM open -- no pre-open auction simulation.
- Grid/trailing levels are evaluated on each 5-minute bar's CLOSE, not tick by tick --
  live checks every 1 second, so an intrabar spike through a level is invisible here.
- Transaction costs ARE modeled (brokerage, STT, exchange charges, SEBI charges, stamp
  duty, GST) on the leveraged order value. Slippage is NOT modeled.
- 18 Sep is today -- if the session isn't fully over yet, or a symbol's last bar or two
  hasn't posted on Yahoo, that day's numbers can still move.
"""


def main() -> None:
    print(f"Fetching Yahoo 5-min intraday bars for {ALL_SYMBOLS} ...")
    bars = yahoo_intraday.fetch_many(ALL_SYMBOLS, period="30d", interval="5m")

    missing = [s for s in ALL_SYMBOLS if s not in bars]
    if missing:
        print(f"WARNING: no data for {missing}")
    if EXCLUDED:
        print(f"EXCLUDED (no resolvable NSE ticker): {EXCLUDED}")

    results = backtest.run_backtest(
        bars,
        daily_plan=DAILY_PLAN,
        grid_pct=GRID_PCT,
        margin_capital=MARGIN_CAPITAL,
        total_units=TOTAL_UNITS,
        max_concurrent_positions=MAX_CONCURRENT_POSITIONS,
        enable_averaging=False,
        trail_stop=True,
        profit_exit=True,
        daily_loss_cap=compute_daily_loss_cap(MARGIN_CAPITAL),
        portfolio_profit_lock_trigger=compute_portfolio_profit_lock_trigger(MARGIN_CAPITAL),
        portfolio_profit_lock_fixed=True,
    )

    print(
        f"\n{'=' * 70}\nSep 2026 picks backtest: {len(DAILY_PLAN)} trading days, "
        f"grid={GRID_PCT:.1%}, leverage={backtest.LEVERAGE}x, margin_capital=Rs {MARGIN_CAPITAL:,}\n{'=' * 70}\n"
    )
    print(backtest.summarize(results, goal_daily_pnl=GOAL_DAILY_PNL))

    print("\n--- Day by day ---")
    import pandas as pd

    daily = pd.DataFrame(results["daily_results"]).sort_values("date")
    trades = pd.DataFrame(results["trade_log"])
    for _, row in daily.iterrows():
        day = row["date"]
        symbols_today = ", ".join(f"{s}({d})" for s, d in DAILY_PLAN.get(day, {}).items())
        note = f"  [{row['note']}]" if isinstance(row.get("note"), str) else ""
        print(f"{day}  {symbols_today:65s} net P&L: Rs {row['pnl']:>10,.2f}{note}")
        if not trades.empty:
            day_trades = trades[trades["date"] == day]
            for _, tr in day_trades.iterrows():
                print(
                    f"    {tr['symbol']:12s} {tr['direction']:5s} avg={tr['avg_price']:.2f} "
                    f"exit={tr['exit_price']:.2f} qty={tr['qty']:.1f} units={tr['units_used']} "
                    f"gross={tr['gross_pnl']:>9,.2f} costs={tr['costs']:>6,.2f} net={tr['pnl']:>9,.2f} ({tr['reason']})"
                )

    print(CAVEATS)


if __name__ == "__main__":
    main()
