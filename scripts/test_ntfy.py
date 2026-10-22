"""Send one test push to your phone. Usage: .venv/bin/python -m scripts.test_ntfy"""
from src import ntfy_notify

if not ntfy_notify.enabled():
    raise SystemExit("NTFY_TOPIC is not set in .env")
ntfy_notify.send("Test alert from your Contabo server. If you see this, phone alerts work.", title="Test alert", priority="high")
print("Sent. Check your phone.")
