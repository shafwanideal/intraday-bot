# Intraday Grid Trading Bot

Grid-averaging intraday strategy for Indian equities via Zerodha Kite Connect.
See the project brief for strategy rules and build status.

**Status**: auth, a real historical backtest, shadow (paper) trading, and
live order placement are working. Live trading is gated behind
`LIVE_TRADING_ENABLED=true` plus a typed confirmation.

Can be run entirely from a phone via the Telegram control bot -- see
"Phone control" below and `deploy/README.md` for the Contabo VPS setup.

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

Kite access tokens expire daily and need a real password + 2FA login. That login is a
**bookmark click**: `kite_login.py serve` runs permanently on the VPS (under PM2), behind
nginx on an HTTPS address that is registered as the Kite app's Redirect URL. Every
successful login writes `KITE_ACCESS_TOKEN` into the VPS `.env` (mode 600, gitignored).

Bookmark: `https://kite.zerodha.com/connect/login?api_key=<KITE_API_KEY>&v=3`
(the API key is not secret; `kite_login.py serve` prints this URL when it starts).

The bot only **reads** that token. If it is missing or stale, `daily.py`, the guardians
and `get_kite()` stop with a "log in via your Kite login bookmark" message -- they never
start a login themselves. Click the bookmark first, then start them. Verify any time with
`.venv/bin/python3 kite_login.py check`.

Set `KITE_USER_ID=<your Zerodha ID>` in `.env` so the public listener only accepts a login
for your own account (the API key is public, so otherwise anyone could log in to the app
with a different Zerodha account and overwrite the saved token).

**Guardians start by themselves.** Right after each successful login, `kite_login.py` runs
`scripts/on_login.sh`, which starts the swing + intraday guardians in the `guardians` tmux
session if it isn't already running (it never restarts a running one, so the intraday
guardian's trailing-stop state is kept). A running guardian picks up the fresh token on its own
(`auth.refresh_client_if_changed`). The hook sets `LIVE_TRADING_ENABLED=true` only inside the
guardians' own process -- `.env` is not changed -- and the guardians can only ever place exit
orders. Its output goes to `logs/on_login.log`. After pulling this change, run
`pm2 restart kite-login-callback` once so the listener loads it.

**Phone alerts (Telegram).** Needs a working `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` in `.env`
(test with `.venv/bin/python3 scripts/send_alert.py --test`). You get a message when: a trailing
stop starts, anything is sold (trailing stop or the 15:08 square-off), a position is picked up or
closed outside the bot, the guardians stop and again when they are back up, the login listener goes
down / comes back, and when the Kite login is still not done at 08:30 (reminders every 10 minutes
until 09:30, then "login received"). The last three come from `scripts/monitor.py`, run under PM2:
`pm2 start .venv/bin/python3 --name algo-monitor --interpreter none -- scripts/monitor.py && pm2 save`.

Server setup (once): see `deploy/README.md`, "Kite login listener (bookmark mode)".
The one-off/tunnel alternative still works while the Redirect URL is
`http://127.0.0.1:8000/`: `ssh -L 8000:127.0.0.1:8000 root@<vps>` then
`kite_login.py --redirect http://127.0.0.1:8000/ --no-browser` on the VPS.

## Pre-flight check

```bash
.venv/bin/python3 scripts/preflight.py
```

Verifies the machine is ready to trade: Python version, packages, `.env` and
its permissions, all four credentials, a live Telegram round-trip (it sends
you a test message), today's Kite session, writable log directory, clock, and
which trading mode the gate is in. Read-only apart from that test message --
it places no orders. Exits 0 when ready, 1 with a list of what to fix.

Run it after setup, and any morning something feels off.

## Phone control (Telegram)

Runs the whole trading day from Telegram, so a VPS can host it with no SSH
session open:

```bash
python3 scripts/run_bot.py     # or under systemd -- see deploy/README.md
```

```
/login          authenticate with Kite (once per trading day)
RVNL long, MAZDOCK short       set today's plan
/confirm  ->  CONFIRM          approve sizing and arm real trading
/status         open positions and live P&L
/add BSE long   add a stock to a running session
/stop           square off everything now
/shadow         paper-trade today's plan instead
```

This changes *where* the confirmation is typed, not whether one is required.
`LIVE_TRADING_ENABLED=true` must still be set in `.env` on the server, and a
human still approves the same sizing summary the terminal would have printed.
The bot ignores messages from any chat other than `TELEGRAM_CHAT_ID`.

Needs `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`.

## Backtest

```bash
python3 scripts/run_backtest.py              # generic sample-symbol backtest
python3 scripts/run_daily_plan_backtest.py   # backtest specific (date, symbol) picks you actually made
python3 scripts/run_bse_today_backtest.py    # one stock, one day (BSE long)
```

Uses Kite Connect 5-min intraday data via `src/kite_data.py`, and `src/strategy.py`'s `GridEngine` for the actual grid
logic (2% averaging/exit by default, 5x leverage, real Zerodha intraday
costs, mark-to-market daily loss cap). Edit `DAILY_PLAN` in
`scripts/run_daily_plan_backtest.py` to test your own dated picks.

These read Kite's feed — the same one live/shadow mode trades on — so they
need a same-day token (click the Kite login bookmark first, see Authenticate above).

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
`rsi_dip`) and source (`kite`, the only one).

These need only **daily** bars, fetched from Kite via `src/kite_data.py`, so
today's Kite login is required. Bars are cached under `.cache/` per fetch-date,
so re-running with different strategy parameters doesn't re-download the universe.

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

## Depth scanner (live alerts, no picks needed)

```bash
python3 -m src.auth              # re-authenticate (daily)
python3 scripts/run_depth_scanner.py
```

Polls `kite.quote()` for the Nifty 200 universe (`src/screener.py`'s list)
every 10s during market hours. A symbol only gets a Telegram alert once
**all three** agree, the same combination that was actually present
before GRANULESIND's two up-legs on 2026-09-11:

1. **Depth** — visible top-5 order book skews >= 85% to one side (buy or
   sell), with a minimum total book size so thin/illiquid names don't
   trigger on noise.
2. **Price** — last traded price is at/within 0.15% of today's high (for
   a buy signal) or low (for a sell signal) — i.e. actually breaking out
   right now, not just up or down on the day.
3. **Volume** — trading pace since the last poll is running at least 3x
   the stock's own average pace since today's open (self-normalizing per
   stock; skipped for the first 5 minutes after open, since the day's
   average pace isn't meaningful yet).

Depth-only candidates that don't clear price/volume are still printed to
the console each poll (for visibility) but don't page you. Read-only: it
only calls `kite.quote()`, never places an order. Each alert re-fires at
most once every 15 minutes per symbol+side while it stays confirmed.

This is a stronger filter than depth alone, not a guarantee — even a
confirmed alert is "go look at the chart," not an auto-entry signal.
Requires `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` in `.env` (see
`src/telegram_notify.py`); without them it just prints alerts to the
console.
