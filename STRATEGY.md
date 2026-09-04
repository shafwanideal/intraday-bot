# Intraday Grid Strategy — Full Specification

Broker-agnostic description of the strategy. The reference implementation in
this repo targets Zerodha Kite, but nothing about the rules below is
Zerodha-specific except the cost model (see "Porting to another broker").

---

## Capital and sizing

| Item | Value |
|---|---|
| Margin capital per day | ₹50,000 |
| Split into | 4 units of ₹12,500 margin each |
| Intraday leverage (MIS) | 5x |
| Actual exposure per unit | ₹62,500 of stock |
| Max concurrent positions | 3 stocks |

The 4 units are a shared pool. Three stocks entering at open use 3 units,
leaving 1 spare for whichever position needs to average first.

## Stock selection

Manual, 1–3 stocks chosen each morning. No auto-screening. Every stock is
traded **long by default**; a stock is only shorted if explicitly flagged
short for that day.

## Entry

- Enter 1 unit per stock at market open (9:15 IST).
- One entry per stock per day. If a position exits, it is **not** re-entered.
- (Reference implementation also allows adding a stock mid-session; it then
  enters at the current price rather than the day's open.)

## Averaging (one leg only)

If price moves **2% against** the position, add 1 more unit at that price,
improving the average cost. This happens **at most once** per position — it
is not a multi-level grid. After averaging, the position holds 2 units and
the 2% exit target is measured from the new blended average price.

## Exit — trailing stop

1. When price moves **2% in favour** of the position, it does **not** exit.
   Instead the trailing stop activates and records that price as the peak.
2. From then on, every new favourable extreme raises (long) or lowers
   (short) the peak.
3. The position exits when price retraces **1.0 × ATR(14)** from that peak,
   in absolute price terms.

ATR(14) is the 14-day Average True Range from daily candles, computed per
stock before the session. This makes the trail width scale with each
stock's own volatility instead of using one fixed percentage for
everything — a stock with 4% daily range gets a wider trail than one with
1.5%.

## Risk controls (hard stops, not suggestions)

- **Daily loss cap**, evaluated mark-to-market (realised P&L + unrealised
  P&L on open positions) on every price poll. Breach → close everything
  immediately and take no further trades that day. Scales with capital:
  ₹3,000 floor for margin capital ≤ ₹1,00,000, +₹500 per extra ₹50,000
  (e.g. ₹3,500 at ₹1,50,000, ₹4,000 at ₹2,00,000).
- **Portfolio profit lock**, same mark-to-market total. Once the day's
  total P&L first reaches ₹3,500, it arms and starts tracking the peak —
  the position keeps riding and the floor trails ₹500 behind that peak
  (so it only ever ratchets up, never down). If total P&L pulls back to
  the floor, close everything immediately and take no further trades that
  day. If exact execution at ₹3,500 proves slippage-prone in practice,
  ₹3,000 is an acceptable fallback trigger — dial it down via
  `PORTFOLIO_PROFIT_LOCK_TRIGGER_OVERRIDE` rather than treating it as a
  hard requirement.
- **Hard square-off at 3:08 PM IST.** Anything still open — waiting,
  averaged, or mid-trail — is closed at market. Nothing is held overnight.
  (Moved earlier from 3:15 PM after Kite itself started rejecting MIS
  orders placed at or after 3:12 PM.)

---

## Validation status — read this before trusting any of it

**This has never been traded live, and has not yet been run against a live
market session even in paper mode.** Everything below is backtest only.

Backtested against real 5-minute intraday data (Zerodha historical API,
~180-day window), with real transaction costs and 5x leverage modelled:

- **119 trading days × 6-stock portfolio**: roughly ₹400–700 net per day
  average depending on configuration. Median day higher than mean — a few
  large losing days drag the average down.
- **~40 individually hand-picked stock/date tests**: win rate mostly
  65–85%, average around ₹1,300–1,600 net per trade.
- Daily loss cap triggered on roughly 3–13% of days depending on config.

### Known weaknesses

- **Parameters were tuned on the same small sample used to judge them.**
  Grid %, loss cap, and ATR multiplier were all selected by testing
  variants against ~40 trades. That is a real overfitting risk — in one
  case an apparent improvement turned out to be a single outlier trade
  carrying the whole result.
- **Tail risk is asymmetric.** Typical wins ran ₹1,000–2,500; the worst
  single loss in testing was ₹3,994. The strategy relies on hit rate, not
  on wins being larger than losses.
- **The stocks tested were not blind picks.** They were chosen because
  there was already reason to think they'd move. Picking cold at 9:00 AM
  with no hindsight is a materially different and harder problem.
- A **₹2,000/day** profit target was tested and found **not reliably
  achievable**. Realistic expectation from the backtest is closer to
  ₹400–700/day average, with high day-to-day variance.

### Findings worth knowing

- **Earnings surprises beat analyst ratings as an intraday signal.**
  Stocks reacting to a quarterly beat produced sharp, tradeable same-day
  moves. Broker "Buy" calls and target-price hikes did not — those get
  priced in gradually, which is useless for a strategy needing a 2% move
  inside one session.
- **Size of the earnings beat did not predict the move.** A stock that
  beat profit estimates by 75% lost money the next session; one that beat
  by 14.5% was among the best performers.
- **Check whether the reaction already happened.** If a result was
  announced during market hours, the move is usually already priced in by
  the next day — trading "the day after" is then trading stale news. Only
  results genuinely released after close reliably produced a next-day move.

---

## Porting to another broker (e.g. Groww)

The strategy rules above are broker-agnostic. Three things change:

**1. Transaction costs.** The reference cost model is Zerodha-specific:
brokerage min(₹20, 0.03% per order), STT 0.025% sell-side, exchange txn
~0.00297%, SEBI charges, stamp duty 0.003% buy-side, 18% GST on
brokerage + exchange charges. Groww's published intraday brokerage is
min(₹20, 0.05% per order) — the statutory charges (STT, exchange, stamp,
GST) are set by the exchange/government and are the same everywhere.
At ₹62,500 per order the difference is small (roughly ₹1–2 per order) but
the cost model should still be corrected before trusting any P&L figure.
**Verify current rates on the broker's own pricing page — these change.**

**2. Leverage.** 5x MIS leverage is an assumption, and real intraday
leverage varies per stock and per broker. Check the broker's own margin
calculator per symbol. If actual leverage is lower, all P&L scales down
proportionally — and so does risk.

**3. API integration.** The reference implementation uses the `kiteconnect`
Python library, which is Zerodha-only. Groww publishes its own trade API
with a Python SDK (`groww.in/trade-api`), priced around ₹499+tax/month at
time of writing — but note that some third-party sources claim Groww does
not permit external algo integration, so **this needs confirming directly
with Groww before building anything against it.** The data layer, order
layer, and auth layer would all need rewriting; the strategy engine itself
(entry/averaging/trailing/loss-cap/square-off logic) is broker-independent
and would port unchanged.

If the API route doesn't work out, every rule above can be executed
manually — the strategy makes at most a handful of decisions per stock per
day, and the only one needing continuous attention is the trailing stop.

---

## Not financial advice

This is an experimental system built and tested over a short period. It has
produced encouraging backtest numbers and has not yet survived contact with
a live market. Anyone running it risks real money and should size
accordingly.
