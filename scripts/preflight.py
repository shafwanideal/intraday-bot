"""Pre-flight check: verify this machine is ready to trade before the market opens.

Run it after setup, and again any morning something feels off:

    .venv/bin/python3 scripts/preflight.py

Every check is read-only and safe to run at any time -- it places no orders and
changes no state. The one thing it does send is a test message to your Telegram
chat, which is the only honest way to prove that path works end to end.

Exit code is 0 if you're ready to trade, 1 if something needs fixing. Written
to be read by someone who is not a programmer: every failure says what to do
about it, not just what went wrong.
"""

import os
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

IST = ZoneInfo("Asia/Kolkata")

# Terminal colours, dropped when output isn't a terminal (piping to a file,
# journalctl) so the log doesn't fill with escape codes.
_TTY = sys.stdout.isatty()
GREEN = "\033[32m" if _TTY else ""
RED = "\033[31m" if _TTY else ""
YELLOW = "\033[33m" if _TTY else ""
DIM = "\033[2m" if _TTY else ""
BOLD = "\033[1m" if _TTY else ""
OFF = "\033[0m" if _TTY else ""

failures: list[str] = []
warnings: list[str] = []


def ok(label: str, detail: str = "") -> None:
    print(f"  {GREEN}PASS{OFF}  {label}" + (f"  {DIM}{detail}{OFF}" if detail else ""))


def fail(label: str, fix: str) -> None:
    print(f"  {RED}FAIL{OFF}  {label}")
    print(f"        {RED}->{OFF} {fix}")
    failures.append(label)


def warn(label: str, note: str) -> None:
    print(f"  {YELLOW}WARN{OFF}  {label}")
    print(f"        {YELLOW}->{OFF} {note}")
    warnings.append(label)


def heading(text: str) -> None:
    print(f"\n{BOLD}{text}{OFF}")


def check_python() -> None:
    heading("Python")
    major, minor = sys.version_info[:2]
    # zoneinfo (3.9+) and the `str | None` syntax used across src/ (3.10+).
    if (major, minor) >= (3, 10):
        ok(f"Python {major}.{minor}", sys.executable)
    else:
        fail(
            f"Python {major}.{minor} is too old",
            "This project needs Python 3.10 or newer. On Debian/Ubuntu: apt install python3.11 python3.11-venv",
        )


def check_packages() -> None:
    heading("Packages")
    required = {
        "kiteconnect": "the Zerodha API client",
        "pandas": "used by the ATR/indicator maths",
        "requests": "used to talk to Telegram",
        "dotenv": "reads your .env file",
    }
    for module, why in required.items():
        try:
            __import__(module)
            ok(module, why)
        except ImportError:
            fail(
                f"{module} is not installed",
                "Run: .venv/bin/pip install -r requirements.txt  (make sure you use .venv/bin/pip, not plain pip)",
            )


def check_env_file() -> None:
    heading("Configuration file")
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        fail(
            ".env not found",
            f"Run: cp {PROJECT_ROOT}/.env.example {env_path} && chmod 600 {env_path} , then fill it in with nano",
        )
        return
    ok(".env exists", str(env_path))

    mode = env_path.stat().st_mode & 0o777
    if mode & 0o077:
        # Not fatal -- the bot runs fine -- but this file holds an API secret
        # that can place orders, and on a shared box that matters.
        warn(
            f".env is readable by other users on this machine (permissions {oct(mode)[2:]})",
            f"Run: chmod 600 {env_path}",
        )
    else:
        ok(".env permissions", f"{oct(mode)[2:]} -- owner only")


def check_credentials() -> None:
    heading("Credentials")
    try:
        from src import config
    except Exception as exc:  # noqa: BLE001
        fail(f"Couldn't load configuration: {exc}", "Check that .env has no stray quotes or spaces around the = signs.")
        return

    if config.KITE_API_KEY:
        ok("KITE_API_KEY is set", f"{config.KITE_API_KEY[:4]}… ({len(config.KITE_API_KEY)} chars)")
    else:
        fail("KITE_API_KEY is missing", "Add it to .env. Find it on your app page at developers.kite.trade")

    if config.KITE_API_SECRET:
        ok("KITE_API_SECRET is set", "(value hidden)")
    elif config.KITE_ACCESS_TOKEN:
        ok("KITE_API_SECRET not needed", "KITE_ACCESS_TOKEN is set, which skips the login exchange")
    else:
        fail("KITE_API_SECRET is missing", "Add it to .env. Find it on your app page at developers.kite.trade")

    if config.TELEGRAM_BOT_TOKEN:
        ok("TELEGRAM_BOT_TOKEN is set", "(value hidden)")
    else:
        fail("TELEGRAM_BOT_TOKEN is missing", "Get one from @BotFather on Telegram, then add it to .env")

    if config.TELEGRAM_CHAT_ID:
        ok("TELEGRAM_CHAT_ID is set", config.TELEGRAM_CHAT_ID)
    else:
        fail(
            "TELEGRAM_CHAT_ID is missing",
            "Message your bot, then open https://api.telegram.org/bot<TOKEN>/getUpdates and copy the chat id",
        )


def check_telegram() -> None:
    heading("Telegram")
    try:
        import requests

        from src import config, telegram_notify
    except Exception as exc:  # noqa: BLE001
        fail(f"Couldn't load the Telegram module: {exc}", "Fix the errors above first.")
        return

    if not telegram_notify.enabled():
        warn("Skipped -- token or chat id missing", "Fix the credential failures above, then run this again.")
        return

    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getMe",
            timeout=15,
        )
    except requests.exceptions.RequestException as exc:
        fail(f"Couldn't reach Telegram: {exc}", "Check this server has internet access: try `curl https://api.telegram.org`")
        return

    if resp.status_code == 401:
        fail("Telegram rejected your bot token", "The token in .env is wrong. Get a fresh one from @BotFather.")
        return
    if resp.status_code != 200:
        fail(f"Telegram returned {resp.status_code}", f"Unexpected response: {resp.text[:200]}")
        return

    bot_name = resp.json().get("result", {}).get("username", "?")
    ok("Bot token is valid", f"@{bot_name}")

    stamp = datetime.now(IST).strftime("%d %b %H:%M:%S IST")
    if telegram_notify.send_message(f"Pre-flight check {stamp} -- if you can read this, Telegram is wired up correctly."):
        ok("Test message sent", "check your phone -- if nothing arrived, the chat id is wrong")
    else:
        fail(
            "Could not send to your chat id",
            "TELEGRAM_CHAT_ID is probably wrong. Message your bot, then check "
            "https://api.telegram.org/bot<TOKEN>/getUpdates for the right id.",
        )


def check_kite_session() -> None:
    heading("Kite session")
    try:
        from src import auth, config
    except Exception as exc:  # noqa: BLE001
        fail(f"Couldn't load the auth module: {exc}", "Fix the errors above first.")
        return

    if not auth.has_valid_session():
        # Expected outside trading hours -- tokens die overnight, so this is
        # normal at 8 AM and only a problem once you actually want to trade.
        warn(
            "No Kite session for today yet",
            "Normal before your morning login. Send /login to the Telegram bot when you're ready to trade.",
        )
        return

    source = "KITE_ACCESS_TOKEN env var" if config.KITE_ACCESS_TOKEN else "today's cached login"
    try:
        profile = auth.get_kite(interactive=False).profile()
        ok(f"Logged in as {profile['user_name']} ({profile['user_id']})", source)
    except Exception as exc:  # noqa: BLE001
        fail(
            f"A session exists but Kite rejected it: {exc}",
            "The token has probably expired. Send /login to the Telegram bot for a fresh one.",
        )


def check_writable() -> None:
    heading("File access")
    for name in ("logs", "."):
        target = PROJECT_ROOT / name
        probe = target / ".preflight_write_test"
        try:
            target.mkdir(exist_ok=True)
            probe.write_text("ok")
            probe.unlink()
            ok(f"{name}/ is writable" if name != "." else "project directory is writable", str(target))
        except OSError as exc:
            fail(
                f"Cannot write to {target} ({exc})",
                "The bot needs to write trade logs and the daily session file here. "
                "Check the directory is owned by the user running the service: chown -R trader:trader ~/intraday-bot",
            )


def check_clock() -> None:
    heading("Clock")
    now_ist = datetime.now(IST)
    ok("Time in IST", now_ist.strftime("%a %d %b %Y, %H:%M:%S"))

    local = datetime.now().astimezone()
    if local.utcoffset() != now_ist.utcoffset():
        warn(
            f"This server's clock is set to {local.tzname()}, not IST",
            "Trading still works correctly (the code converts internally), but server logs "
            "won't match your Telegram timestamps. Fix with: sudo timedatectl set-timezone Asia/Kolkata",
        )
    else:
        ok("Server timezone is IST", "server logs will match your Telegram messages")

    if now_ist.weekday() >= 5:
        print(f"        {DIM}(today is {now_ist.strftime('%A')} -- markets are closed){OFF}")


def check_trading_gate() -> None:
    heading("Trading mode")
    try:
        from src import config
    except Exception:  # noqa: BLE001
        return

    if config.LIVE_TRADING_ENABLED:
        print(f"  {YELLOW}LIVE{OFF}  Real orders are ENABLED. /confirm will spend real money.")
        print(f"        {DIM}To go back to paper trading, remove LIVE_TRADING_ENABLED from .env and restart the service.{OFF}")
    else:
        print(f"  {GREEN}SAFE{OFF}  Real orders are disabled -- shadow (paper) mode only.")
        print(f"        {DIM}This is the right setting until shadow mode has earned your trust.{OFF}")


def main() -> int:
    print(f"{BOLD}Intraday bot -- pre-flight check{OFF}")
    print(f"{DIM}{PROJECT_ROOT}{OFF}")

    check_python()
    check_packages()
    check_env_file()
    check_credentials()
    check_telegram()
    check_kite_session()
    check_writable()
    check_clock()
    check_trading_gate()

    print()
    if failures:
        noun = "thing needs" if len(failures) == 1 else "things need"
        print(f"{RED}{BOLD}NOT READY{OFF} -- {len(failures)} {noun} fixing:")
        for item in failures:
            print(f"  - {item}")
        print("\nFix those, then run this again.")
        return 1

    if warnings:
        noun = "note" if len(warnings) == 1 else "notes"
        print(f"{GREEN}{BOLD}READY{OFF} -- with {len(warnings)} {noun}:")
        for item in warnings:
            print(f"  - {item}")
        return 0

    print(f"{GREEN}{BOLD}READY{OFF} -- everything checks out.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
