"""Diagnostic: print the live arm threshold, breakeven%, and which mode
(same-day intraday-style vs carried swing-breakeven-style -- see
swing_live.py's own docstring) currently applies to one or more real MTF
positions, computed fresh from Kite + persisted state -- not guessed from
memory.

Usage:
    .venv/bin/python3 scripts/check_arm_threshold.py BEL
    .venv/bin/python3 scripts/check_arm_threshold.py BEL ADANIENT
    .venv/bin/python3 scripts/check_arm_threshold.py          # every tracked/open symbol
"""

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import auth, swing_live  # noqa: E402
from src.swing_strategy import PROFIT_TARGET_PCT, TRAILING_PCT, compute_mtf_breakeven_pct  # noqa: E402


def main() -> None:
    requested = set(sys.argv[1:])
    state = json.loads(swing_live.SWING_STATE_FILE.read_text()) if swing_live.SWING_STATE_FILE.exists() else {}
    kite = auth.get_kite()
    positions = {
        p["tradingsymbol"]: p
        for p in kite.positions()["net"]
        if p.get("product") == "MTF" and p.get("quantity", 0) > 0
    }
    today = date.today()
    symbols = requested or (set(state.keys()) | set(positions.keys()))

    for symbol in sorted(symbols):
        st = state.get(symbol)
        pos = positions.get(symbol)
        if not st or not pos:
            print(f"{symbol}: not currently tracked / no open MTF position")
            continue

        avg = pos["average_price"]
        qty = pos["quantity"]
        breakeven_pct = None

        if swing_live._is_same_day_entry(st, today):
            arm_pct = swing_live._same_day_arm_threshold_pct(swing_live._now())
            trail_pct = swing_live.SAME_DAY_TRAIL_PCT
            mode = "same-day (intraday-style)"
        else:
            entry = date.fromisoformat(st["entry_date"])
            days_held = max(0, (today - entry).days)
            funded_amount = st.get("funded_amount")
            if funded_amount is not None:
                breakeven_pct = compute_mtf_breakeven_pct(qty * avg, funded_amount, days_held)
                arm_pct = breakeven_pct + PROFIT_TARGET_PCT
            else:
                arm_pct = PROFIT_TARGET_PCT
            trail_pct = TRAILING_PCT
            mode = "carried (swing breakeven-style)"

        arm_price = avg * (1 + arm_pct)
        breakeven_str = f"breakeven_pct={breakeven_pct * 100:.3f}% " if breakeven_pct is not None else ""
        print(
            f"{symbol}: mode={mode} avg={avg:.2f} qty={qty} entry_date={st['entry_date']} days_held="
            f"{(today - date.fromisoformat(st['entry_date'])).days} {breakeven_str}"
            f"arm_pct={arm_pct * 100:.3f}% arm_price={arm_price:.2f} trail_pct={trail_pct * 100:.2f}% "
            f"trailing={st['trailing']} peak_price={st.get('peak_price')} floor_price={st.get('floor_price')}"
        )


if __name__ == "__main__":
    main()
