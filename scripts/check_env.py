"""Safe .env check: prints each credential-like setting's NAME, length and whether it is
plain ASCII. Never prints a value.   .venv/bin/python -m scripts.check_env"""
import sys
from pathlib import Path

from dotenv import dotenv_values

root = Path(__file__).resolve().parent.parent
vals = dotenv_values(root / ".env")
print(f"Reading {root / '.env'}")
for k, v in vals.items():
    if not k.startswith(("GROWW", "NTFY", "KITE", "TELEGRAM")):
        continue
    v = v or ""
    bad = [i for i, ch in enumerate(v) if ord(ch) > 126 or ord(ch) < 33]
    status = "EMPTY" if not v else ("OK ascii" if not bad else f"BAD chars at positions {bad[:5]}")
    print(f"  {k:<22} len={len(v):<5} {status}")
