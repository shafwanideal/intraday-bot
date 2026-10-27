#!/bin/bash
# One command for the daily guardian startup routine -- pulls the latest
# code and starts BOTH guardians (swing/MTF and intraday/manual MIS)
# together in a SINGLE detached tmux session ("guardians"), sharing one
# Kite login. Replaces the old two-session, two-login setup (separate
# "intraday" and "swing" sessions) per explicit instruction that the daily
# login had become a chore -- one terminal, one login, both guardians.
#
# Run this AS THE trader USER, from anywhere:
#   ~/intraday-bot/scripts/start_guardians.sh
#
# Or as a single line right after SSH'ing in as root:
#   sudo -iu trader bash -lc '~/intraday-bot/scripts/start_guardians.sh'
#
# Idempotent: if the "guardians" session already exists, it's left untouched
# rather than restarted. The intraday guardian's armed/trailing state is NOT
# persisted across restarts (see src/intraday_live.py's own docstring -- MIS
# never carries overnight, so this is deliberate) -- a reflexive re-run of
# this script later in the day must never silently wipe live tracking state
# for a position that's already armed.
#
# Click your Kite login bookmark FIRST (see README, "Bookmark mode") so the guardians find
# a live token in .env; they never start a login themselves and stop with a clear message.

set -e
cd ~/intraday-bot

echo "Pulling latest code..."
git pull origin main-47itwf

echo "Ensuring LIVE_TRADING_ENABLED=true in .env..."
.venv/bin/python3 - <<'PYEOF'
from pathlib import Path

env_file = Path(".env")
new_line = "LIVE_TRADING_ENABLED=true"
lines = env_file.read_text().splitlines() if env_file.exists() else []
for i, line in enumerate(lines):
    if line.strip().startswith("LIVE_TRADING_ENABLED="):
        lines[i] = new_line
        break
else:
    lines.append(new_line)
env_file.write_text("\n".join(lines) + "\n")
PYEOF

# Retire the old two-session setup if either is still running, so there is
# never a double-watcher on the same position (the old sessions and the new
# combined one would otherwise both try to manage the same real trades).
for old in intraday swing; do
    if tmux has-session -t "$old" 2>/dev/null; then
        echo "Stopping old '$old' session (replaced by the combined 'guardians' session)..."
        tmux kill-session -t "$old"
    fi
done

if tmux has-session -t guardians 2>/dev/null; then
    echo "tmux session 'guardians' already exists -- leaving it alone (not restarting, so live tracking state isn't lost)."
else
    echo "Starting both guardians in a new tmux session..."
    tmux new-session -d -s guardians -c ~/intraday-bot ".venv/bin/python3 scripts/run_all_guardians.py"
fi

echo
echo "Done. If today's Kite login isn't done yet, click your Kite login bookmark BEFORE the guardians start."
echo "Then: tmux attach -t guardians   # Ctrl+B D to detach"
