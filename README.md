# Intraday Grid Trading Bot

Grid-averaging intraday strategy for Indian equities via Zerodha Kite Connect.
See the project brief for strategy rules and build status.

**Status**: auth, a real historical backtest, and shadow (paper) trading
against live Kite data are working. Nothing here places real orders yet.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in your Kite API key/secret
```

Note: `cryptography` is pinned to `48.0.1` in requirements.txt — newer versions
don't ship prebuilt wheels for Intel Mac + Python 3.14, which breaks the
`pyOpenSSL` dependency of `kiteconnect` unless you have a full Rust/OpenSSL
toolchain installed.

## Authenticate

Kite access tokens expire daily, so this must be run once each trading morning:

```bash
python3 -m src.auth
```

This prints a login URL, waits for you to paste back the redirect URL (or
just the `request_token` from it) after logging in, then exchanges it for an
access token and caches it in `.kite_session.json` (gitignored) for the rest
of the day. Other modules should call `src.auth.get_kite()` to get an
authenticated client — it reuses the cached token if still valid for today.

That login needs a browser and someone at the keyboard, so it can't run in a
headless or cloud session. For those, generate the token where you *can* log
in (`python3 -m src.auth` prints `KITE_ACCESS_TOKEN=...` alongside caching it)
and set it as an environment variable there:

```bash
export KITE_ACCESS_TOKEN=...   # today's token, from the login above
export KITE_API_KEY=...        # KITE_API_SECRET is NOT needed for this path
```

`get_kite()` prefers that variable over both the cache and the login flow, and
validates it up front so an expired one fails immediately with a clear message
rather than mid-backtest. Kite kills tokens overnight, so it's a fresh value
each trading day — and it's a live credential that can place orders, not just
read data, so treat it like one.

## Backtest

```bash
python3 scripts/run_backtest.py              # generic sample-symbol backtest
python3 scripts/run_daily_plan_backtest.py   # backtest specific (date, symbol) picks you actually made
python3 scripts/run_bse_today_backtest.py    # one stock, one day (BSE long), no Kite login needed
```

Uses free Yahoo Finance 5-min intraday data (last ~60 days only) via
`src/data.py`, and `src/strategy.py`'s `GridEngine` for the actual grid
logic (2% averaging/exit by default, 5x leverage, real Zerodha intraday
costs, mark-to-market daily loss cap). Edit `DAILY_PLAN` in
`scripts/run_daily_plan_backtest.py` to test your own dated picks.

These read Kite's feed — the same one live/shadow mode trades on — so they
need a same-day token (see Authenticate above). `scripts/run_bse_today_backtest.py`
also takes `--source yahoo`, which falls back to `src/yahoo_intraday.py`
(Yahoo's chart endpoint over plain `requests`, same DataFrame shape as
`src/kite_data.py`, including a 14-day ATR helper) when no token is available
at all. That's a different vendor's prices, so numbers won't match live mode
exactly — the script says which source it used.

### Swing backtests (entry triggers)

Separate from the intraday grid: multi-day positions, unlimited averaging
legs, no square-off, delivery (CNC) costs. See `src/swing_strategy.py`.

```bash
python3 scripts/compare_entry_triggers.py                        # all triggers, side by side
python3 scripts/compare_entry_triggers.py 200000 252 nifty200 1500000
python3 scripts/run_52w_low_backtest.py 200000 252 nifty50 1500000 rsi_dip
```

Args are positional: capital per leg, entry-window length in trading days,
universe (`nifty50` | `nifty200` | `nifty500`), max total capital (`none`
for uncapped), and for the single-trigger script a trigger (`52w_low` |
`rsi_dip`) and source (`yahoo` | `kite`).

These need only **daily** bars, which Yahoo serves free and without auth via
`src/yahoo_daily.py` — so unlike the intraday backtests they run with no Kite
login at all. Bars are cached under `.cache/` per fetch-date, so re-running
with different strategy parameters doesn't re-download the universe.

## Shadow mode (paper trading against live data)

Each morning, once you have today's picks (max 3, since that's the
strategy's concurrent-position cap):

```bash
cp todays_stocks.example.json todays_stocks.json   # first time only
# edit todays_stocks.json with today's symbols + "long"/"short"
python3 -m src.auth                                 # re-authenticate (daily)
python3 scripts/run_shadow.py
```

This polls live Kite quotes every 15s during market hours and runs the same
`GridEngine` logic as the backtest, logging every decision (entry,
averaging, target exit, daily loss cap, square-off) to
`logs/shadow_YYYY-MM-DD.jsonl` and the console. **It never calls any
order-placement endpoint — nothing real trades.** `todays_stocks.json` is
gitignored since it changes every day.

You don't have to know your full list by 9:15 — the script re-reads
`todays_stocks.json` on every poll, so you can add a symbol any time
(up to the 3-slot cap) while it's already running, no restart needed. A
symbol present from the start enters at that day's real open price; one
added later enters at whatever the price is right then (using the day's
open wouldn't make sense for a symbol you only decided on at 10 AM). No
new entries are taken after 2:30 PM, since there's not enough of the day
left for the strategy to do anything with a fresh position.
