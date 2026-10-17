# MTF 52-Week-Low Strategy — Full Specification

Positional (multi-day) MTF strategy. **Not implemented in this repo and not
wired into the live bot** — `src/live.py` and `src/strategy.py` run the
intraday grid described in [STRATEGY.md](STRATEGY.md). This document records a
backtested specification only.

Validated on Nifty 100 daily bars (Kite historical) over
**16 Oct 2024 → 16 Oct 2026**.

---

## Capital and sizing

| Item | Value |
|---|---|
| Wallet | ₹10,00,000 |
| MTF leverage | 4.4x |
| Exposure per leg | ₹50,000 |
| Own capital per leg | ₹11,364 |
| Max averages per position | 3 (so max ₹2,00,000 exposure / ₹45,455 own per stock) |
| Funding cost | 14%/yr on the funded portion, exposure × (1 − 1/4.4) |

Own capital tied up averaged ₹1,38,924 over the test and peaked at
**₹5,56,818**. The wallet must stay free — signals cluster, see "Risks".

## Stock selection

Automatic. Universe is the **Nifty 100** (`data/nifty100.csv`). There is no
discretionary pick and no ranking — the entry trigger does the selecting.

## Entry

1. **Trigger** — the stock's `Low` is at or below the lowest `Low` of the
   prior **252 trading sessions** (a new 52-week low).
2. **Filter** — the 20-day mean of `(High − Low) / Close` must be **≥ 2.5%**,
   measured on the signal day.
3. **Execution** — buy at the **next session's open**. Never the signal day;
   the signal is only known after the low has printed.

One position per stock. While a position is open, further 52-week lows do
**not** add to it. After a position closes, a fresh 52-week low may re-enter.

## Averaging

Add one more ₹50,000 leg every time price trades **3% below the running
average price**, up to a **hard maximum of 3 averages**. Each add is the same
rupee size — never doubled.

The 3-average cap is not a guideline. Uncapped averaging drove the test
account to negative equity (−₹27,672 and −₹2,32,981 in separate runs).

## Exit

Trailing stop only. There is **no stop loss** and no profit target.

1. **Arm** when the high reaches `avg × (1 + breakeven% + 2%)`.
2. **Breakeven%** = `(buy costs + estimated sell cost + accrued interest) / exposure`.
   Recompute daily — it grows as interest accrues, so a position held 90 days
   has a materially higher arm threshold than a fresh one.
3. **Trail** at `running peak high − 3 × ATR(14)` (Wilder) once armed.

Arming off a breakeven that excludes accrued interest will book losses as
profits. This is the same class of bug fixed in `check_portfolio_profit_lock`
(commit `fef3ee0`).

## Cost model

Delivery (CNC) charges, both legs:

| Charge | Rate |
|---|---|
| Brokerage | 0.03%, capped at ₹20 |
| STT | 0.1% **buy and sell** |
| Stamp duty | 0.015%, buy side only |
| Exchange transaction | 0.00297% |
| SEBI turnover | 0.0001% |
| GST | 18% on brokerage + exchange |

The ₹20 cap binds on every ₹50,000 leg, so brokerage is ~₹141/leg and
effectively irrelevant. STT is 72% of non-interest charges — 6.7× the
intraday rate, which is 0.025% sell-side only.

## Backtested result

₹10,00,000 wallet, position size fixed throughout (no compounding).

| | |
|---|---|
| Net P&L | **+₹4,30,528** (+43.1% over 2 years, ~21.5%/yr) |
| Gross P&L | +₹5,52,421 |
| Charges | −₹30,501 |
| MTF interest | −₹91,391 |
| Trades | 104 (215 buy legs) |
| Win rate | 56.7% |
| Average win / loss | +₹11,124 / −₹5,017 |
| Median holding period | 34 days |
| Max drawdown | 15.9% (28 Feb 2025) |
| Lowest equity | ₹9,31,486 |
| Year 1 / Year 2 | +₹2,24,122 / +₹2,06,406 |
| Return on capital actually deployed | 310% |

Interest is **75% of total cost**. The only levers on it are shorter holds
and less borrowing.

## Risks

1. **Single-trade concentration.** ADANIENT contributed ₹1,23,095 — 29% of
   net. Excluding it the two-year return is +30.7%.
2. **Signals cluster in selloffs.** Counts by six-month block were 74, 6, 49,
   58. The strategy is dormant for months, then commits heavily while the
   market falls. Peak own capital ₹5,56,818.
3. **Thin sample.** 104 trades, two years, one regime containing two
   selloffs — the conditions this setup suits.
4. **Idle capital.** ₹8,61,076 unused on average, but the full wallet must
   stay available for the clustered peaks.

## Rejected variants

Each was tested and failed; recorded so they are not reintroduced.

| Variant | Outcome |
|---|---|
| Doubling the averaging ladder | 0/24 starts positive, 91% max DD, worst equity ₹33,403 |
| Uncapped averaging | Negative equity: −₹27,672 and −₹2,32,981 |
| Any hard price stop loss (−8% to −15%) | −₹48,414 to −₹1,05,299; raised max DD 20.5% → 24.5% |
| Arming at breakeven+5% | Best single year of the study, then 29% DD and −₹1,73,044 open losses in H2 — fitted |
| 52-week **high** entry (momentum) | Lost in both halves across all 15 variants; breakout entry and average-down are contradictory |
| Top 10 / top 25 most volatile universe | Starves the trigger — 2 trades in year two |
| Raising the filter above 2.5% | Monotonic loss: ₹4,30,528 → ₹2,94,146 → ₹1,31,715 → ₹69,700 |
| Lowering the filter to 2.0% | +₹64,523 but max DD 16.2% → 23.2%; the marginal 2.0–2.5% trades earn ₹1,115 each vs ₹4,240 |
| Nifty 25 megacap universe | −₹80,597 median; too slow to clear breakeven+2% |
| 200-DMA, momentum rank, sector rotation, 13-filter screen | None survived out of sample |

## Validation method

Two independent one-year halves, each starting from a fresh ₹10,00,000:

| | H1 (Oct 24 → Oct 25) | H2 (Oct 25 → Oct 26) |
|---|---|---|
| Net P&L | +₹2,24,122 | +₹2,06,406 |
| Win rate | 57.1% | 56.2% |
| Max drawdown | 15.9% | 16.2% |

Agreement across both halves on every metric is why this configuration was
chosen over higher-scoring but period-dependent alternatives.

## Implementation notes

- The scan runs **after market close**; orders are placed pre-market for the
  next open.
- Needs 252 sessions of prior daily history per symbol to compute the
  52-week low. Note `kite.historical_data(days=...)` counts **calendar**
  days — request ~1,100 days to obtain ~750 trading rows.
- Averaging depth must be enforced per position, not globally.
- This strategy shares the Kite API key with the live intraday bot. Refreshing
  the access token invalidates the live session, so re-auth only after
  15:30 IST on a trading day.
