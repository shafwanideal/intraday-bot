import json
import time as time_module
from datetime import date, datetime, time
from pathlib import Path

from kiteconnect.exceptions import KiteException

from . import auth
from .strategy import SQUARE_OFF_TIME, GridEngine

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
POLL_INTERVAL_SECONDS = 15

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TODAYS_STOCKS_FILE = PROJECT_ROOT / "todays_stocks.json"
LOG_DIR = PROJECT_ROOT / "logs"


def load_todays_plan() -> dict[str, str]:
    if not TODAYS_STOCKS_FILE.exists():
        raise RuntimeError(
            f"{TODAYS_STOCKS_FILE} not found. Copy todays_stocks.example.json to "
            "todays_stocks.json and fill in today's picks before running shadow mode."
        )
    with open(TODAYS_STOCKS_FILE) as f:
        plan = json.load(f)
    if not plan:
        raise RuntimeError(f"{TODAYS_STOCKS_FILE} is empty -- add at least one symbol.")
    if len(plan) > 3:
        raise RuntimeError(
            f"{TODAYS_STOCKS_FILE} has {len(plan)} symbols but max concurrent positions is 3. Trim the list."
        )
    for symbol, direction in plan.items():
        if direction not in ("long", "short"):
            raise RuntimeError(f"Invalid direction '{direction}' for {symbol} -- must be 'long' or 'short'.")
    return plan


class ShadowLogger:
    def __init__(self, log_path: Path):
        self.log_path = log_path
        log_path.parent.mkdir(exist_ok=True)

    def event(self, kind: str, **fields) -> None:
        record = {"timestamp": datetime.now().isoformat(), "kind": kind, **fields}
        print(f"[{record['timestamp']}] {kind}: {fields}")
        with open(self.log_path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")


def run_shadow(poll_interval: int = POLL_INTERVAL_SECONDS) -> None:
    """Paper-trade the grid strategy against LIVE Kite quotes for today's plan.
    Logs every decision (entry, averaging, target exit, loss cap, square-off)
    -- it never calls any order-placement endpoint, so nothing real trades.
    """
    plan = load_todays_plan()
    kite = auth.get_kite()

    today = date.today()
    logger = ShadowLogger(LOG_DIR / f"shadow_{today.isoformat()}.jsonl")
    engine = GridEngine()
    instruments = [f"NSE:{sym}" for sym in plan]
    entered_today: set[str] = set()

    print(f"SHADOW MODE (paper trading, no real orders) -- {today}")
    print(f"Plan: {plan}")
    print(f"Log: {logger.log_path}\n")
    logger.event("start", plan=plan, grid_pct=engine.grid_pct, exposure_per_unit=engine.exposure_per_unit)

    try:
        while True:
            now = datetime.now().time()
            if now < MARKET_OPEN:
                time_module.sleep(min(poll_interval, 30))
                continue
            if now >= MARKET_CLOSE:
                print("Market closed. Ending shadow session.")
                break

            try:
                quotes = kite.ohlc(instruments)
            except KiteException as exc:
                logger.event("poll_error", error=str(exc))
                time_module.sleep(poll_interval)
                continue

            current_prices: dict[str, float] = {}
            for symbol in plan:
                quote = quotes.get(f"NSE:{symbol}")
                if quote is None:
                    continue
                current_prices[symbol] = quote["last_price"]

                if symbol not in entered_today:
                    direction = plan[symbol]
                    open_price = quote["ohlc"]["open"]
                    if engine.enter(symbol, open_price, direction, datetime.now()):
                        entered_today.add(symbol)
                        logger.event("entry", symbol=symbol, direction=direction, price=open_price)

            for symbol, price in current_prices.items():
                result = engine.update(symbol, price, datetime.now())
                if result:
                    logger.event(result["reason"], **result)

            for result in engine.check_loss_cap(current_prices, datetime.now()):
                logger.event("daily_loss_cap", **result)

            if now >= SQUARE_OFF_TIME and engine.open_positions:
                for result in engine.square_off(current_prices, datetime.now()):
                    logger.event("square_off", **result)

            logger.event(
                "heartbeat",
                daily_pnl=round(engine.daily_pnl, 2),
                open_positions=list(engine.open_positions.keys()),
                halted=engine.halted,
            )

            if not engine.open_positions and (engine.halted or now >= SQUARE_OFF_TIME):
                print("All positions flat and day is done (halted or past square-off). Ending session.")
                break

            time_module.sleep(poll_interval)
    except KeyboardInterrupt:
        print("\nInterrupted by user.")

    logger.event("end", daily_pnl=round(engine.daily_pnl, 2), trades=len(engine.trade_log))
    print(f"\nShadow session ended. Net P&L for today (paper): Rs {engine.daily_pnl:,.2f}")
    print(f"Full log: {logger.log_path}")
