# intraday-bot — context for a new Claude Code session

This file exists so a **new session** (especially one running locally, e.g.
directly on the VPS via `claude remote-control` or SSH + `claude`) can pick
up this project with zero lost context. Read this before touching anything.

## What this is

A live Zerodha Kite Connect algorithmic trading bot for the user (S Mohammed
Shafwan, Zerodha ID GZP870), deployed on a Contabo VPS at **217.216.59.103**,
Linux user **`trader`**, repo path `~/intraday-bot` (i.e.
`/home/trader/intraday-bot`). GitHub: `shafwanideal/intraday-bot`, branch
`main-47itwf`. This coding environment's checkout has no live Kite
credentials — all live-API verification happens by giving the user exact
shell commands to run over SSH and reading back screenshots.

## Two parts — DO NOT confuse them

**Part 1 — the exit-only guardian ("the sell bot").** Files:
`src/swing_live.py`, `src/intraday_live.py`, `src/hourly_pnl.py`,
`src/digest.py`, `src/alerts.py`, `scripts/run_all_guardians.py`,
`src/swing_strategy.py`, `src/strategy.py`. Watches every MTF/MIS position,
places trailing-stop and crash-stop-loss EXIT orders automatically. **Never
places an entry.** The user explicitly, repeatedly instructed: this must
not be modified without asking first, even when a new feature seems to
require it — find a way to avoid touching it (see pdf_entry_live.py's own
docstring for the GTT-conversion trick that was used instead of touching
swing_live.py's GTT-detection logic).

**Part 2 — `src/pdf_entry_live.py` + `scripts/run_pdf_entry_bot.py`.** A
separate, isolated process (own state file `logs/pdf_entry_state.json`, own
log `logs/pdf_entry_<date>.jsonl`, own env gate
`PDF_ENTRY_TRADING_ENABLED=true`). Two ways a stock enters Part 2:
1. **Pick-based**: user gives a Kush-Bohra-style pick (symbol+SL+T1+T2) via
   PDF or chat, queued into `logs/pending_picks.json`. User buys manually
   (MTF); bot detects the buy, splits 50/50 into two OCO GTTs (hard sell at
   T1, other half floors to the ORIGINAL BUY PRICE and trails 0.5% toward
   T2 — NOT floored at T1, per explicit "protect my capital" instruction).
2. **Auto-adopt** (no PDF/chat needed at all): user just buys and places
   their OWN plain single-leg stop-loss GTT. `_scan_for_auto_adopt` finds
   it, converts it to an OCO (same SL + a placeholder target that should
   never fire) — THIS is what makes Part 1 stand down with zero changes to
   Part 1, since Part 1's `fetch_manual_gtt_info` only recognises two-leg
   GTTs. T1 = breakeven%+2%, T2 = breakeven%+5%. Full state machine is in
   `pdf_entry_live.py`'s own module docstring — read it before changing
   anything in the auto-adopt flow.

Both parts run in **separate tmux sessions** on the VPS: `guardians` (Part
1, via `scripts/run_all_guardians.py`) and `pdf_entry` (Part 2, via
`scripts/run_pdf_entry_bot.py`). Never merge them.

## Deploying a change (the loop so far has been)

```
sudo -iu trader bash -c "cd ~/intraday-bot && git pull origin main-47itwf"
# then stop+restart whichever tmux session changed:
sudo -iu trader tmux attach -t pdf_entry   # or: guardians
# Ctrl+C (a REAL keypress while attached — `tmux send-keys ... C-c` has
# repeatedly failed to actually interrupt the process; don't rely on it)
cd ~/intraday-bot && .venv/bin/python3 scripts/run_pdf_entry_bot.py
# Ctrl+B then D to detach without killing it
```

Tmux sessions were originally created via `tmux new-session -d -s NAME` then
`tmux send-keys -t NAME "..." Enter` (NOT by launching the python command as
the session's direct argument) — this matters: a session whose pane runs
the python process directly dies entirely when that process exits, instead
of leaving a reusable shell behind.

## Known gotchas already hit in production (2026-11-23, first live day)

1. **NSE tick size is per-instrument, not a universal 0.05.** Fixed via
   `_get_tick_size` (fetches `kite.instruments("NSE")`, caches per symbol).
   APOLLOHOSP uses 0.50. If ANY other GTT price error shows up mentioning
   "tick size", this is probably already handled — check `_get_tick_size`'s
   cache/fallback logic first before assuming a new bug.
2. User frequently runs commands in their **local Mac terminal** instead of
   SSH'd into the VPS (`sudo: unknown user trader` is the tell). Always
   confirm the prompt shows `root@vmi3512211` or `trader@vmi3512211` before
   trusting a command's output.
3. `tmux send-keys ... C-c` sent in rapid succession with other send-keys
   commands can race / fail to interrupt — prefer telling the user to
   attach and press Ctrl+C themselves for anything that must actually stop
   a running process before the next step.

## Open/in-progress as of this handoff

**SGFIN mystery (unresolved):** auto-adopted at 200 shares @ avg ₹865.75,
SL ₹826.8. Kite now shows the live position as 100 shares @ avg ₹858.70,
LTP ₹806 (well below the SL) — but it has NOT been sold. Suspected cause:
the auto-adopt flow captures `qty`/`avg_price` ONCE at detection
(`_scan_for_auto_adopt`) and never re-syncs them against
`kite.positions()` afterward, unlike Part 1's `swing_live.py` which always
re-reads qty fresh each poll. If the user's real holding changed shape
after adoption (partial sell, averaging, etc.), the GTT this module is
managing is sized for the OLD quantity and may be silently mismatched with
the real position. This is a real, not-yet-fixed gap in
`pdf_entry_live.py`'s auto-adopt phases — next step is to confirm via (a)
Kite's GTT list for SGFIN's actual current quantity/trigger/status, and
(b) `grep '"symbol": "SGFIN"' logs/pdf_entry_*.jsonl` on the VPS, then
likely add a qty-reconciliation step (re-read `kite.positions()` each poll
and reconcile, the way `swing_live.py` already does) to every auto-adopt
phase handler.

## Test discipline

Every change in this project has been tested with mocked `kite` objects
before deploying (scratchpad test files, not committed — e.g.
`test_pdf_entry_live.py`, `test_pdf_entry_auto_adopt.py`,
`test_tick_rounding.py`, `test_instrument_tick_size.py`). Write and run an
equivalent test before pushing any further change to `pdf_entry_live.py`.
