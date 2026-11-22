"""Put the Groww TOTP token + secret into .env WITHOUT echoing them.

    .venv/bin/python -m scripts.set_groww_keys

Prompts (hidden input -- paste with Cmd+V, nothing shows on screen), checks the
values are plain ASCII with no stray spaces/line breaks, writes them into .env
(keeping every other line), removes any stale GROWW_ACCESS_TOKEN / GROWW_API_*
lines (they would override the TOTP login), and prints only lengths.
"""
import getpass
import os
import re
import sys
import tempfile
from pathlib import Path

ENV = Path(__file__).resolve().parent.parent / ".env"
DROP = ("GROWW_ACCESS_TOKEN", "GROWW_API_KEY", "GROWW_API_SECRET")


def ask(label: str, min_len: int) -> str:
    while True:
        raw = getpass.getpass(f"Paste {label} (hidden) then Enter: ")
        val = re.sub(r"\s+", "", raw)  # remove any spaces / line breaks from pasting
        if not val:
            print("  empty -- try again")
        elif not val.isascii():
            print("  contains non-plain characters (maybe a '...' or smart quote) -- re-copy it and try again")
        elif len(val) < min_len:
            print(f"  only {len(val)} characters, looks cut off -- re-copy it and try again")
        else:
            print(f"  OK, {len(val)} characters")
            return val


token = ask("the TOTP TOKEN (the long one, starts eyJ)", 200)
secret = ask("the TOTP SECRET (the short one under the QR code)", 16)

lines = ENV.read_text().splitlines() if ENV.exists() else []
keep = [ln for ln in lines if not ln.split("=", 1)[0].strip() in DROP + ("GROWW_TOTP_TOKEN", "GROWW_TOTP_SECRET")]
keep += [f"GROWW_TOTP_TOKEN={token}", f"GROWW_TOTP_SECRET={secret}"]

fd, tmp = tempfile.mkstemp(dir=ENV.parent, prefix=".env.")
with os.fdopen(fd, "w") as f:
    f.write("\n".join(keep) + "\n")
os.chmod(tmp, 0o600)
os.replace(tmp, ENV)
print(f"\nSaved to {ENV}. Removed any old GROWW_ACCESS_TOKEN / GROWW_API_* lines.")
