#!/bin/bash
# Run by kite_login.py right after every successful Kite login (the bookmark click):
# makes sure the swing + intraday guardians are running in the "guardians" tmux session.
#
# - If the session is already running it is LEFT ALONE: restarting would wipe the intraday
#   guardian's armed/trailing state. A running guardian picks up the fresh token by itself
#   (auth.refresh_client_if_changed), so nothing needs restarting after a re-login.
# - LIVE_TRADING_ENABLED=true is set only in the guardians' own process environment, so the
#   gate in .env is NOT flipped. The guardians can only ever place EXIT orders on positions
#   that already exist -- they never buy.
# - No git pull here: logging in never changes the code that is running.
set -u
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
stamp() { date '+%Y-%m-%d %H:%M:%S'; }

if ! command -v tmux >/dev/null 2>&1; then
    echo "$(stamp) tmux not found -- guardians NOT started"; exit 1
fi
if tmux has-session -t guardians 2>/dev/null; then
    echo "$(stamp) guardians session already running -- leaving it alone"; exit 0
fi
tmux new-session -d -s guardians -c "$PWD" "LIVE_TRADING_ENABLED=true bash scripts/run_guardians_supervised.sh"
echo "$(stamp) started guardians session"
