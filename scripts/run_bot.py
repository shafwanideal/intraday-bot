"""Entry point for the phone-driven control bot (systemd runs this)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.telegram_control import run_control_bot

if __name__ == "__main__":
    run_control_bot()
