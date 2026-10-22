#!/bin/bash
# Installs the Groww guardian as a systemd service so it starts at boot and
# restarts if it crashes. Run as root from the repo:
#   bash deploy/install_groww_service.sh
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"
[ -x "$DIR/.venv/bin/python" ] || { echo "No $DIR/.venv -- set up the venv first."; exit 1; }

# Don't run two guardians at once (double alerts, shared state file).
tmux kill-session -t groww 2>/dev/null || true

sed "s#__DIR__#$DIR#g" "$DIR/deploy/groww-guardian.service" > /etc/systemd/system/groww-guardian.service
systemctl daemon-reload
systemctl enable groww-guardian.service
systemctl restart groww-guardian.service
sleep 4
systemctl --no-pager --lines=0 status groww-guardian.service | head -5
echo
echo "Last log lines:"
journalctl -u groww-guardian.service -n 5 --no-pager | cut -c1-200
echo
echo "Useful: systemctl status groww-guardian | journalctl -u groww-guardian -f | systemctl stop groww-guardian"
