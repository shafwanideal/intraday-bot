#!/bin/bash
# Run as root:  bash /home/trader/intraday-bot/deploy/install_groww_holdings_service.sh
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"
[ -x "$DIR/.venv/bin/python" ] || { echo "No $DIR/.venv"; exit 1; }
# Retire the old, broken MTF watcher (it pointed at a folder that no longer exists).
systemctl disable --now groww-guardian.service 2>/dev/null || true
rm -f /etc/systemd/system/groww-guardian.service
sed "s#__DIR__#$DIR#g" "$DIR/deploy/groww-holdings-guardian.service" > /etc/systemd/system/groww-holdings-guardian.service
systemctl daemon-reload
systemctl enable groww-holdings-guardian.service
systemctl restart groww-holdings-guardian.service
sleep 5
systemctl --no-pager --lines=0 status groww-holdings-guardian.service | head -5
echo; echo "Last log lines:"
journalctl -u groww-holdings-guardian.service -n 8 --no-pager | cut -c1-220
echo; echo "Useful: journalctl -u groww-holdings-guardian -f | systemctl stop groww-holdings-guardian"
