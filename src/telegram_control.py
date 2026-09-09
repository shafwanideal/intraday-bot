"""Phone-driven control surface for the live trading bot.

Runs as a long-lived process (systemd on the VPS) and long-polls Telegram
for commands, so an entire trading day can be driven from a phone with no
SSH session:

    /login          -> get the Kite OAuth link, paste the redirect back
    RVNL long, MAZDOCK short
                    -> stage today's plan, get the approval summary
    CONFIRM         -> arm; real orders start at 9:15
    /status         -> open positions, live P&L
    /add SYM long   -> add a stock to a session already running
    /stop           -> square off everything now and end the day
    /shadow         -> run the same plan in paper mode instead

WHAT THIS DOES NOT DO: it does not remove a single safety gate.
LIVE_TRADING_ENABLED must still be true in .env, and a human still has to
type CONFIRM against the same summary the terminal would have printed --
this only changes which keyboard it gets typed on. Nothing here ever
auto-approves a plan.

Threading model: Telegram polling owns the main thread so the bot keeps
answering /status and /stop while a session is live; the trading loop runs
on one worker thread. The two share exactly three things -- a
threading.Event for stop, a threading.Event for approval, and a read-only
reference to the GridEngine for /status -- rather than any shared mutable
plan state, because the plan's authoritative home is already the JSON file
that live.py re-reads on every poll.
"""

import json
import threading
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import auth, config, live, shadow, telegram_notify

IST = ZoneInfo("Asia/Kolkata")
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Deliberately the same files live.py/shadow.py already read, and kept
# separate from each other for the same reason live.py documents: editing the
# paper-trading list must never be able to change what real orders get placed.
LIVE_PLAN_FILE = live.TODAYS_STOCKS_FILE
SHADOW_PLAN_FILE = shadow.TODAYS_STOCKS_FILE

MAX_STOCKS = live.MAX_STOCKS_PER_DAY
CONFIRM_PHRASE = live.CONFIRM_PHRASE

HELP_TEXT = """Intraday bot — commands

/login — authenticate with Kite (needed once each trading day)
/picks RVNL long, MAZDOCK short — set today's plan
   (a plain message of symbols works too, no /picks needed)
/confirm — approve the staged plan and arm real trading
/status — open positions and live P&L
/add SYMBOL long — add a stock to a running session
/stop — square off everything now and end the day
/shadow — run today's plan in paper mode (no real orders)
/help — this message

Max {max_stocks} stocks per day. Long is the default if you omit
the direction, so "RVNL" and "RVNL long" mean the same thing.""".format(max_stocks=MAX_STOCKS)


def _now() -> datetime:
    return datetime.now(IST)


def parse_picks(text: str) -> dict[str, str]:
    """Parse a free-typed plan into {SYMBOL: "long"|"short"}.

    Accepts what someone actually thumbs into a phone -- commas or newlines
    between stocks, direction optional, any case:
        "RVNL long, MAZDOCK short"
        "rvnl, mazdock"
        "RVNL LONG\nBSE short"

    Raises ValueError with a human-readable reason rather than guessing, so
    a typo'd direction can never be silently traded as a long.
    """
    text = text.strip()
    for prefix in ("/picks", "/plan"):
        if text.lower().startswith(prefix):
            text = text[len(prefix) :]
    # Newlines and commas are both separators; semicolons because phone
    # keyboards make them easy to hit by accident next to comma.
    for sep in ("\n", ";"):
        text = text.replace(sep, ",")

    plan: dict[str, str] = {}
    for chunk in text.split(","):
        parts = chunk.split()
        if not parts:
            continue
        symbol = parts[0].upper().strip()
        if not symbol.replace("&", "").replace("-", "").isalnum():
            raise ValueError(f"'{parts[0]}' doesn't look like an NSE symbol.")
        if len(parts) == 1:
            direction = "long"
        elif len(parts) == 2:
            direction = parts[1].lower().strip()
        else:
            raise ValueError(f"Couldn't read '{chunk.strip()}' — expected 'SYMBOL' or 'SYMBOL long/short'.")
        if direction in ("l", "buy"):
            direction = "long"
        elif direction in ("s", "sell"):
            direction = "short"
        if direction not in ("long", "short"):
            raise ValueError(f"'{direction}' isn't a direction for {symbol} — use long or short.")
        if symbol in plan and plan[symbol] != direction:
            raise ValueError(f"{symbol} is listed both long and short.")
        plan[symbol] = direction

    if not plan:
        raise ValueError("No stocks found in that message.")
    if len(plan) > MAX_STOCKS:
        raise ValueError(f"{len(plan)} stocks given but the daily max is {MAX_STOCKS}.")
    return plan


class ControlBot:
    """Telegram front end. One instance per process."""

    def __init__(self):
        self.offset: int | None = None
        self.staged_plan: dict[str, str] | None = None
        self.awaiting_token = False
        self.session_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        # Set by the trading thread once it has built the approval summary;
        # the main thread sends it and waits on approval_event.
        self.approval_event = threading.Event()
        self.approved = False
        self.pending_summary: str | None = None
        self.engine = None
        self.mode: str | None = None  # "live" | "shadow" | None

    # -- outbound ----------------------------------------------------------

    def say(self, text: str) -> None:
        telegram_notify.send_message(text)

    # -- session state -----------------------------------------------------

    @property
    def session_running(self) -> bool:
        return self.session_thread is not None and self.session_thread.is_alive()

    # -- command handlers --------------------------------------------------

    def handle(self, text: str) -> None:
        stripped = text.strip()
        lowered = stripped.lower()

        # A pending Kite login consumes the next message wholesale -- a
        # redirect URL contains '/' and would otherwise be misread as a command.
        if self.awaiting_token and not lowered.startswith("/"):
            self.finish_login(stripped)
            return

        if lowered in ("/start", "/help", "help"):
            self.say(HELP_TEXT)
        elif lowered == "/login":
            self.start_login()
        elif lowered == "/status":
            self.report_status()
        elif lowered == "/stop":
            self.request_stop()
        elif lowered == "/shadow":
            self.start_session(mode="shadow")
        elif lowered in ("/confirm", CONFIRM_PHRASE.lower()):
            self.handle_confirm()
        elif lowered.startswith("/add"):
            self.handle_add(stripped[len("/add") :])
        elif lowered.startswith("/"):
            self.say(f"Unknown command: {stripped.split()[0]}\n\n{HELP_TEXT}")
        else:
            self.stage_plan(stripped)

    def start_login(self) -> None:
        if auth.has_valid_session():
            self.say("Already authenticated for today. Send today's picks, or /login again to force a new token.")
        try:
            url = auth.build_login_url()
        except RuntimeError as exc:
            self.say(f"Can't build the login URL: {exc}")
            return
        self.awaiting_token = True
        self.say(
            "Kite login — tap this link, log in, then send me back the URL "
            "you land on (the page will look broken; that's expected — its "
            "address is what I need):\n\n" + url
        )

    def finish_login(self, raw: str) -> None:
        try:
            auth.complete_login(raw)
        except (RuntimeError, ValueError) as exc:
            self.say(f"Login failed: {exc}\n\nSend /login to try again.")
            return
        self.awaiting_token = False
        try:
            profile = auth.get_kite(interactive=False).profile()
            who = f"{profile['user_name']} ({profile['user_id']})"
        except Exception as exc:  # noqa: BLE001 - the token saved; a profile call failing is not fatal
            who = f"(couldn't read profile: {exc})"
        self.say(f"Authenticated as {who}. Send today's picks — e.g. 'RVNL long, MAZDOCK short'.")

    def stage_plan(self, text: str) -> None:
        try:
            plan = parse_picks(text)
        except ValueError as exc:
            self.say(f"{exc}\n\nSend something like: RVNL long, MAZDOCK short")
            return

        if self.session_running:
            self.say(
                "A session is already running. Use /add SYMBOL long to add to it, "
                "or /stop to end the day first."
            )
            return

        self.staged_plan = plan
        pretty = "\n".join(f"  {sym} {direction}" for sym, direction in plan.items())
        self.say(
            f"Staged plan ({len(plan)} stock(s)):\n{pretty}\n\n"
            "Send /confirm to arm real trading (I'll show you the full sizing "
            "summary and ask once more), or /shadow to paper-trade it instead."
        )

    def handle_add(self, arg: str) -> None:
        if not self.session_running:
            self.say("No session running — send your picks first.")
            return
        try:
            addition = parse_picks(arg)
        except ValueError as exc:
            self.say(f"{exc}\n\nUse: /add SYMBOL long")
            return

        plan_file = LIVE_PLAN_FILE if self.mode == "live" else SHADOW_PLAN_FILE
        try:
            current = json.loads(plan_file.read_text()) if plan_file.exists() else {}
        except json.JSONDecodeError as exc:
            self.say(f"Couldn't read the current plan file: {exc}")
            return
        merged = {**current, **addition}
        if len(merged) > MAX_STOCKS:
            self.say(f"That would make {len(merged)} stocks; the daily max is {MAX_STOCKS}.")
            return
        plan_file.write_text(json.dumps(merged, indent=2))
        # No restart needed: live.py re-reads this file on every poll and
        # fetches ATR for any symbol it hasn't seen before.
        self.say(f"Added {', '.join(addition)}. Running plan is now: {merged}")

    def handle_confirm(self) -> None:
        # Two distinct meanings, and confusing them would be dangerous: before
        # a session exists CONFIRM means "start one", and mid-startup it means
        # "I approve the sizing summary you just showed me".
        if self.pending_summary is not None:
            self.approved = True
            self.approval_event.set()
            self.say("Approved — arming now.")
            return
        if self.session_running:
            self.say("A session is already running. /status to see it, /stop to end it.")
            return
        if not self.staged_plan:
            self.say("Nothing staged. Send today's picks first — e.g. 'RVNL long, MAZDOCK short'.")
            return
        self.start_session(mode="live")

    def start_session(self, mode: str) -> None:
        if self.session_running:
            self.say("A session is already running.")
            return
        if not self.staged_plan:
            self.say("Nothing staged. Send today's picks first.")
            return
        if not auth.has_valid_session():
            self.say("No Kite session for today — send /login first.")
            return
        if mode == "live" and not config.LIVE_TRADING_ENABLED:
            self.say(
                "LIVE_TRADING_ENABLED is not 'true' in .env on the server, so real orders are "
                "refused. That gate is deliberate and can only be changed on the box itself. "
                "Use /shadow to paper-trade in the meantime."
            )
            return

        plan_file = LIVE_PLAN_FILE if mode == "live" else SHADOW_PLAN_FILE
        plan_file.write_text(json.dumps(self.staged_plan, indent=2))
        self.mode = mode
        self.stop_event.clear()
        self.approval_event.clear()
        self.approved = False
        self.session_thread = threading.Thread(target=self._run_session, args=(mode,), daemon=True)
        self.session_thread.start()
        self.say(f"Starting {mode} session for {self.staged_plan}...")
        self.staged_plan = None

    def _confirm_via_telegram(self, summary: str) -> bool:
        """Approval gate, relocated to the phone. Blocks the trading thread
        until a human answers -- it never times out into an approval. If the
        user never replies, no order is ever placed, which is the correct
        failure direction."""
        self.pending_summary = summary
        self.say(summary + f"\n\nReply {CONFIRM_PHRASE} to place real orders, /stop to abort.")
        self.approval_event.wait()
        self.pending_summary = None
        return self.approved

    def _run_session(self, mode: str) -> None:
        try:
            if mode == "shadow":
                shadow.run_shadow()
            else:
                live.run_live(
                    confirm_fn=self._confirm_via_telegram,
                    notifier=self.say,
                    should_stop=self.stop_event.is_set,
                )
        except Exception as exc:  # noqa: BLE001 - report to the phone, don't die silently
            self.say(f"⚠️ Session crashed: {exc}\n\nCheck the server logs. Any open positions are NOT closed.")
            traceback.print_exc()
        finally:
            self.pending_summary = None
            self.engine = None
            self.say(f"{mode.capitalize()} session thread has ended.")

    def request_stop(self) -> None:
        # Also serves as "abort" while an approval is pending -- releasing the
        # waiter with approved still False means run_live returns before it
        # places anything.
        if self.pending_summary is not None:
            self.approved = False
            self.approval_event.set()
            self.say("Aborted — no orders placed.")
            return
        if not self.session_running:
            self.say("No session running.")
            return
        self.stop_event.set()
        self.say("Stop requested — squaring off all open positions at market. I'll confirm each fill.")

    def report_status(self) -> None:
        lines = [f"Time: {_now().strftime('%d %b %Y %H:%M:%S IST')}"]
        lines.append(f"Kite session today: {'yes' if auth.has_valid_session() else 'NO — send /login'}")
        lines.append(f"Live trading gate: {'enabled' if config.LIVE_TRADING_ENABLED else 'DISABLED (shadow only)'}")
        if not self.session_running:
            lines.append("Session: not running")
            if self.staged_plan:
                lines.append(f"Staged (unconfirmed): {self.staged_plan}")
            self.say("\n".join(lines))
            return

        lines.append(f"Session: {self.mode} running")
        plan_file = LIVE_PLAN_FILE if self.mode == "live" else SHADOW_PLAN_FILE
        if plan_file.exists():
            lines.append(f"Plan: {plan_file.read_text().strip()}")
        try:
            kite = auth.get_kite(interactive=False)
            positions = kite.positions()["day"]
            open_positions = [p for p in positions if p["quantity"] != 0]
            if open_positions:
                lines.append("\nOpen positions (from Kite):")
                for pos in open_positions:
                    lines.append(
                        f"  {pos['tradingsymbol']} qty={pos['quantity']} "
                        f"avg={pos['average_price']:.2f} ltp={pos['last_price']:.2f} "
                        f"P&L={pos['pnl']:,.2f}"
                    )
            else:
                lines.append("\nNo open positions right now.")
            lines.append(f"\nDay P&L (all positions, from Kite): Rs {sum(p['pnl'] for p in positions):,.2f}")
        except Exception as exc:  # noqa: BLE001 - status must never crash the bot
            lines.append(f"\nCouldn't read positions from Kite: {exc}")
        self.say("\n".join(lines))

    # -- main loop ---------------------------------------------------------

    def run(self) -> None:
        if not telegram_notify.enabled():
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must both be set in .env to run the control bot."
            )
        print("Control bot started. Listening for Telegram commands.")
        self.say(
            f"\U0001f916 Control bot online ({_now().strftime('%d %b %H:%M IST')}).\n\n"
            f"Live trading gate: {'ENABLED' if config.LIVE_TRADING_ENABLED else 'disabled (shadow only)'}\n"
            f"Kite session today: {'yes' if auth.has_valid_session() else 'NO — send /login'}\n\n"
            "Send /help for commands."
        )
        while True:
            for update in telegram_notify.get_updates(offset=self.offset):
                self.offset = update["update_id"] + 1
                message = update.get("message") or update.get("edited_message")
                if not message:
                    continue
                chat_id = message.get("chat", {}).get("id")
                if not telegram_notify.is_authorized(chat_id):
                    # Do not reply. A reply confirms the bot exists to whoever
                    # is probing it; silence is the only safe response here.
                    print(f"WARNING: dropped message from unauthorized chat {chat_id}")
                    continue
                text = message.get("text")
                if not text:
                    continue
                try:
                    self.handle(text)
                except Exception as exc:  # noqa: BLE001 - one bad command must not kill the bot
                    self.say(f"Something went wrong handling that: {exc}")
                    traceback.print_exc()


def run_control_bot() -> None:
    ControlBot().run()
