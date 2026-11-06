"""Read-only: does this Groww API key expose order/trade HISTORY (needed for
realized P&L -- "what sold today and at what price", the Groww equivalent of
kite.orders() in src/hourly_pnl.py)? Places NO orders.

Unlike scripts/groww_check.py and groww_check_data.py, this doesn't guess
exact method names up front -- it lists every method on the live SDK object
whose name mentions order/trade/history, then tries calling each no-argument
one, so it adapts to whatever this installed growwapi version actually
exposes instead of going stale if Groww renames something.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.groww_client import get_groww  # noqa: E402

g = get_groww()
print("Logged in.")

candidates = sorted(m for m in dir(g) if not m.startswith("_") and re.search(r"order|trade|history", m, re.I))
print(f"\n== Candidate methods on this SDK object ==\n{candidates}\n")

for name in candidates:
    fn = getattr(g, name)
    if not callable(fn):
        continue
    try:
        result = fn()
        print(f"OK (no args)     {name}(): {str(result)[:300]}")
    except TypeError as exc:
        print(f"NEEDS ARGS       {name}(): {exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED           {name}(): {exc!r}"[:300])
