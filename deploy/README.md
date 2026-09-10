# Running on the Contabo VPS

The bot runs as one long-lived systemd service. You drive the whole trading
day from Telegram on your phone — no SSH after the initial setup.

## Why a VPS at all

Your laptop sleeping at 11 AM with an open MIS position and no square-off is
the failure this removes. The VPS stays up, keeps polling, and squares off at
3:15 whether or not your phone has signal.

## Time zone

The strategy hardcodes IST market hours (9:15 open, 3:15 square-off, 3:30
close) via `zoneinfo`, so the code is correct regardless of the server clock.
Set the box to IST anyway so `journalctl` timestamps line up with what
Telegram tells you:

```bash
sudo timedatectl set-timezone Asia/Kolkata
```

## One-time setup

```bash
sudo adduser --disabled-password --gecos "" trader
sudo -iu trader

git clone <your-repo-url> ~/intraday-bot
cd ~/intraday-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
chmod 600 .env          # it holds your Kite API secret
nano .env               # fill in the four values below
```

`.env` needs:

```
KITE_API_KEY=...
KITE_API_SECRET=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

`LIVE_TRADING_ENABLED=true` is the gate for real orders. **Leave it out
until you've watched shadow mode run for a while.** Without it the bot
works normally but refuses to place real orders — `/shadow` still paper
trades against live prices.

### Getting the Telegram values

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → it gives you
   the `TELEGRAM_BOT_TOKEN`.
2. Send your new bot any message.
3. `curl https://api.telegram.org/bot<TOKEN>/getUpdates` → the
   `message.chat.id` in the response is your `TELEGRAM_CHAT_ID`.

That chat id is a security control, not just a delivery address: the bot
drops every message from any other chat without replying. Anyone who finds
your bot's handle can message it, and the bot can place real orders — so if
you ever change Telegram accounts, update this.

## Check it before wrapping it in a service

```bash
sudo -iu trader
cd ~/intraday-bot
.venv/bin/python3 scripts/preflight.py
```

This catches a bad token or a typo'd chat id now, at a terminal that shows you
the error, rather than later inside systemd where the failure is a line in
`journalctl` you have to go looking for. It sends a test message to your phone
as proof the Telegram path actually works end to end.

Fix anything it reports, re-run until it says READY, then `exit` back to your
sudo user.

## Install the service

```bash
exit                    # back to your sudo user
sudo cp /home/trader/intraday-bot/deploy/intraday-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now intraday-bot
sudo systemctl status intraday-bot
```

You should get a "Control bot online" message on Telegram within seconds.

```bash
journalctl -fu intraday-bot     # follow the logs
```

## Your trading day, from the phone

| Time | You | Bot |
|---|---|---|
| ~8:45 | `/login` | sends the Kite login link |
| | tap it, log in, send back the URL you land on | "Authenticated as ..." |
| | `RVNL long, MAZDOCK short` | shows the staged plan |
| | `/confirm` | shows full sizing summary, asks for CONFIRM |
| | `CONFIRM` | arms — orders go in at 9:15 |
| during | `/status` any time | positions and live P&L from Kite |
| during | `/add BSE long` | adds a stock without a restart |
| 3:15 | — | squares off, reports net P&L |
| any time | `/stop` | squares off everything now and ends the day |

### The daily login is unavoidable

Kite access tokens expire every calendar day, and Kite has no way to issue
one without a browser login. The `/login` flow is the least-friction version
that doesn't require putting your Zerodha password and TOTP secret on a
rented server. That tradeoff is deliberate: a token that expires nightly is
a much smaller loss than full account access if the VPS is ever compromised.

The page you land on after logging in will look broken (`127.0.0.1:8000`
refused to connect). That is expected — nothing is listening there. Its
**address bar** is what carries the `request_token`; copy the whole URL and
send it to the bot.

To make this one tap smoother later, you can change the Redirect URL on
developers.kite.trade to point at a small HTTPS endpoint on this box, so the
token lands on the server directly with nothing to copy. Not built yet.

### Alternative: mint the token on your laptop

`get_kite()` also accepts a `KITE_ACCESS_TOKEN` environment variable, which
takes precedence over both the cache and the login flow. You can run
`python3 -m src.auth` on your own machine (where the browser callback just
works), copy the `KITE_ACCESS_TOKEN=...` it prints, and set it on the VPS.

That path also means `KITE_API_SECRET` never has to live on the VPS at all —
the secret only signs the login exchange. The tradeoff is that it needs an
edit plus `systemctl restart intraday-bot` every morning, where `/login` is
just two messages. Worth it if you'd rather keep the API secret off the box.

## Updating

```bash
sudo -iu trader
cd ~/intraday-bot && git pull
.venv/bin/pip install -r requirements.txt
exit
sudo systemctl restart intraday-bot
```

Restarting mid-session ends the trading loop but does **not** close open
positions. `run_live` reconciles real positions from Kite when it starts
again, so re-confirming picks up where it left off — but check `/status`
after any restart with money on the table.

## If something goes wrong

First stop, always:

```bash
sudo -iu trader && cd ~/intraday-bot && .venv/bin/python3 scripts/preflight.py
```

It names the specific problem and the command that fixes it. Most setup
failures are a typo in `.env` or a chat id from the wrong Telegram account.


The bot sends a `⚠️ CRITICAL` message whenever a real order didn't confirm
and the engine's view of a position may not match reality. That message
means **open Kite and look**, not "the bot will sort it out". The engine
halts itself in that state on purpose.

If the VPS dies outright with positions open, nothing squares them off at
3:15 — Zerodha's own auto-square-off (around 3:20) becomes your backstop,
at whatever price it gets. That is the residual risk of running unattended,
and it is the reason `/status` is worth a glance during the day.
