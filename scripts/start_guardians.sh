#!/bin/bash
# One command for the daily guardian startup routine -- pulls the latest
# code and starts both the swing (MTF) and intraday (manual MIS) guardians,
# each in its own detached tmux session ("swing" and "intraday").
#
# Run this AS THE trader USER, from anywhere:
#   ~/intraday-bot/scripts/start_guardians.sh
#
# Or as a single line right after SSH'ing in as root:
#   sudo -iu trader bash -lc '~/intraday-bot/scripts/start_guardians.sh'
#
# Idempotent: if a tmux session already exists under either name, it's left
# untouched rather than restarted. The intraday guardian's armed/trailing
# state is NOT persisted across restarts (see src/intraday_live.py's own
# docstring -- MIS never carries overnight, so this is deliberate) -- a
# reflexive re-run of this script later in the day must never silently wipe
# live tracking state for a position that's already armed.
#
# Still needs YOUR OWN action after this: each session needs an interactive
# Kite login the first time today (Kite tokens expire every night). Attach
# to each and paste the redirect URL when prompted:
#   tmux attach -t intraday   (Ctrl+B then D to detach once logged in)
#   tmux attach -t swing      (Ctrl+B then D to detach once logged in)

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

start_if_missing() {
    local name="$1"
    local script="$2"
    if tmux has-session -t "$name" 2>/dev/null; then
        echo "tmux session '$name' already exists -- leaving it alone (not restarting, so live tracking state isn't lost)."
    else
        echo "Starting '$name' guardian in a new tmux session..."
        tmux new-session -d -s "$name" -c ~/intraday-bot ".venv/bin/python3 scripts/$script"
    fi
}

start_if_missing intraday run_intraday_live.py
start_if_missing swing run_swing_live.py

echo
echo "Done. Attach to each session to complete today's Kite login (first time only):"
echo "  tmux attach -t intraday   # then Ctrl+B D to detach once logged in"
echo "  tmux attach -t swing      # then Ctrl+B D to detach once logged in"
