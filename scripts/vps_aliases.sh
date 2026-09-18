# Shell shortcuts for running the bot without retyping cd/venv paths every
# morning. One-time setup on the VPS:
#
#   echo 'source ~/intraday-bot/scripts/vps_aliases.sh' >> ~/.bashrc
#   source ~/.bashrc
#
# After any `git pull` that touches this file, re-run:
#   source ~/intraday-bot/scripts/vps_aliases.sh
# to pick up changes to the shortcuts themselves (a plain new shell also
# picks them up automatically via .bashrc).

BOT_DIR="$HOME/intraday-bot"
BOT_PY="$BOT_DIR/.venv/bin/python3"

bot-pull() {
    (cd "$BOT_DIR" && git pull origin main-47itwf)
}

# Attaches to the existing trading tmux session if one is running, otherwise
# creates a new one already sitting in the bot's directory. Detach with
# Ctrl+B then D -- the session (and anything running in it) keeps going.
bot-tmux() {
    tmux attach -t trading 2>/dev/null || tmux new -s trading -c "$BOT_DIR"
}

bot-preflight() {
    (cd "$BOT_DIR" && "$BOT_PY" scripts/preflight.py)
}

# Morning routine: git pull, edit today's picks, then straight to live
# trading -- see scripts/daily.py's own docstring for details.
bot-morning() {
    (cd "$BOT_DIR" && "$BOT_PY" scripts/daily.py "$*")
}

bot-live() {
    (cd "$BOT_DIR" && "$BOT_PY" scripts/run_live.py)
}

bot-shadow() {
    (cd "$BOT_DIR" && "$BOT_PY" scripts/run_shadow.py)
}

# Usage: bot-test SYMBOL   (defaults to JYOTICNC if omitted)
bot-test() {
    (cd "$BOT_DIR" && "$BOT_PY" scripts/test_live_order.py "$1")
}

# Usage: bot-add SYMBOL BUY|SELL QUANTITY
bot-add() {
    (cd "$BOT_DIR" && "$BOT_PY" scripts/manual_add.py "$1" "$2" "$3")
}
