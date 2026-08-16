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

## Backtest

```bash
python3 scripts/run_backtest.py             # generic sample-symbol backtest
python3 scripts/run_daily_plan_backtest.py   # backtest specific (date, symbol) picks you actually made
```

Uses free Yahoo Finance 5-min intraday data (last ~60 days only) via
`src/data.py`, and `src/strategy.py`'s `GridEngine` for the actual grid
logic (2% averaging/exit by default, 5x leverage, real Zerodha intraday
costs, mark-to-market daily loss cap). Edit `DAILY_PLAN` in
`scripts/run_daily_plan_backtest.py` to test your own dated picks.

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
