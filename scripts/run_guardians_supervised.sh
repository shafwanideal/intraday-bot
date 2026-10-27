#!/bin/bash
# Keeps the guardians alive: runs scripts/run_all_guardians.py and, if it ever exits (a crash,
# or a stale/missing Kite token at start), starts it again after a short pause. Meant to be the
# command inside the "guardians" tmux session. Ctrl+C (or `tmux kill-session -t guardians`)
# stops it for good -- that is the only way it ends.
#
# A restart re-reads the saved Kite token from .env, so once you have clicked the login bookmark
# a loop that was waiting on a stale token starts the guardians by itself. The swing guardian
# reloads its saved state on start; the intraday guardian reloads today's saved state (see
# src/intraday_live.run_intraday_guardian), so a crash restart does not forget armed stops.
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
DELAY="${GUARDIAN_RESTART_DELAY:-15}"
trap 'echo "stopping supervisor"; exit 0' INT TERM
mkdir -p .cache
while true; do
    .venv/bin/python3 scripts/run_all_guardians.py
    rc=$?
    # One alert per outage, not one every 15 seconds. run_all_guardians clears the marker and
    # sends "back up" once it is running again.
    if [ ! -f .cache/guardians_down ]; then
        touch .cache/guardians_down
        .venv/bin/python3 scripts/send_alert.py "🔴 Guardians STOPPED (exit code $rc). Restarting automatically every ${DELAY}s. If you have not logged in today, click the Kite login bookmark." >/dev/null 2>&1 || true
    fi
    echo "$(date '+%Y-%m-%d %H:%M:%S') run_all_guardians exited (code $rc) -- restarting in ${DELAY}s" | tee -a logs/guardians_supervisor.log
    sleep "$DELAY"
done
