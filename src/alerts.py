"""Human-readable phone messages for the guardians' events.

The loggers used to send `kind: {raw dict}`. This turns the events you actually care about --
trailing stop started, something sold, a position picked up or closed outside the bot -- into
short plain messages. Anything unrecognised falls back to the old raw form, and a formatting
error can never stop an alert from going out.
"""


def _money(x) -> str:
    try:
        return f"₹{float(x):,.2f}"
    except (TypeError, ValueError):
        return str(x)


def format_event(kind: str, fields: dict, label: str = "") -> str:
    tag = f"[{label}] " if label else ""
    try:
        sym = fields.get("symbol", "?")
        if kind.endswith("_sell_suppressed"):
            trigger = {"crash_stop_loss": "the CRASH stop-loss", "trailing_stop": "the trailing stop"}.get(
                fields.get("trigger"), fields.get("trigger", "a sell rule")
            )
            extra = f" ({abs(float(fields['day_move_pct'])):.2f}% today)" if fields.get("day_move_pct") is not None else ""
            return (f"⚠️ {tag}{sym}: a sell WAS triggered by {trigger}{extra}, but it's on your "
                    f"do-not-sell list for today, so it was NOT placed. You are holding this with NO automated "
                    f"downside protection right now -- that's the trade-off of the override.")
        if kind.endswith("_trailing_armed"):
            return (f"📈 {tag}Trailing stop STARTED — {sym}\n"
                    f"Armed at {_money(fields['armed_at'])} ({float(fields['arm_pct']):.2f}% in profit vs avg {_money(fields['avg_price'])}). "
                    f"The stop now follows the peak.")
        if kind.endswith("_crash_stop_loss_exit"):
            qty, price, avg = fields.get("qty"), fields.get("price"), fields.get("avg_price")
            day_move = float(fields.get("day_move_pct", 0))
            pnl = fields.get("gross_pnl")
            pnl_str = f", gross P&L {_money(pnl)} before charges" if pnl is not None else ""
            return (f"🚨🚨 {tag}CRASH STOP-LOSS — {sym} SOLD 🚨🚨\n"
                    f"Down {abs(day_move):.2f}% TODAY (prev close {_money(fields.get('prev_close'))} → {_money(price)}).\n"
                    f"{qty} @ {_money(price)} (avg {_money(avg)}){pnl_str}")
        if kind.endswith("_exit") or kind.endswith("_square_off"):
            direction = fields.get("direction", "long")
            qty, price, avg = fields.get("qty"), fields.get("price"), fields.get("avg_price")
            verb = "SOLD" if direction == "long" else "BOUGHT BACK (short covered)"
            is_stop_loss = kind.endswith("_stop_loss_exit")
            if kind.endswith("_square_off"):
                why = " at the 15:08 square-off"
            elif is_stop_loss:
                why = " by the STOP-LOSS"
            else:
                why = " by the trailing stop"
            emoji = "🛑" if is_stop_loss else "✅"
            line = f"{emoji} {tag}{verb}{why} — {sym}\n{qty} @ {_money(price)}"
            if "gross_pnl" in fields:
                line += f" (avg {_money(avg)}), gross P&L {_money(fields['gross_pnl'])} before charges"
            elif avg is not None and price is not None and qty is not None:
                gross = (float(price) - float(avg)) * float(qty) * (1 if direction == "long" else -1)
                line += f" (avg {_money(avg)}), gross P&L ≈ {_money(gross)} before charges"
            return line
        if kind.endswith("_big_move"):
            pct = float(fields["move_pct"])
            verb = "in your favor" if pct >= 0 else "against you"
            return (f"⚡ {tag}Big move — {sym}\n"
                    f"{abs(pct):.2f}% {verb} vs avg {_money(fields['avg_price'])} (now {_money(fields.get('ltp'))}).")
        if kind.endswith("_position_detected"):
            return f"👀 {tag}Now watching {sym}: {fields.get('qty')} @ avg {_money(fields.get('avg_price'))}"
        if kind.endswith("_position_closed_externally"):
            return f"ℹ️ {tag}{sym} was closed outside the bot — no longer tracked."
        if kind == "start":
            tracked = ", ".join(fields.get("tracked") or []) or "nothing yet"
            return f"▶️ {tag}Guardian started — tracking: {tracked}"
        if kind == "end":
            return f"⏹ {tag}Guardian stopped."
    except Exception:  # noqa: BLE001 - never let formatting block an alert
        pass
    return f"{tag}{kind}: {fields}"
