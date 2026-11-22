"""Set ONE .env value with hidden input (nothing echoes). Usage:
    .venv/bin/python -m scripts.set_env_value NTFY_TOPIC
"""
import getpass
import os
import re
import sys
import tempfile
from pathlib import Path

ENV = Path(__file__).resolve().parent.parent / ".env"
if len(sys.argv) != 2 or not re.fullmatch(r"[A-Z][A-Z0-9_]*", sys.argv[1]):
    raise SystemExit("Usage: python -m scripts.set_env_value NAME")
name = sys.argv[1]
val = re.sub(r"\s+", "", getpass.getpass(f"Paste value for {name} (hidden) then Enter: "))
if not val or not val.isascii():
    raise SystemExit("Empty or non-plain characters -- nothing saved.")
lines = [ln for ln in (ENV.read_text().splitlines() if ENV.exists() else []) if ln.split("=", 1)[0].strip() != name]
lines.append(f"{name}={val}")
fd, tmp = tempfile.mkstemp(dir=ENV.parent, prefix=".env.")
with os.fdopen(fd, "w") as f:
    f.write("\n".join(lines) + "\n")
os.chmod(tmp, 0o600)
os.replace(tmp, ENV)
print(f"Saved {name} ({len(val)} characters) to {ENV}.")
