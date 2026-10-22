# MTF 52-Week-Low Strategy — Full Specification

Positional (multi-day) MTF strategy. **Not implemented in this repo and not
wired into the live bot** — `src/live.py` and `src/strategy.py` run the
intraday grid described in [STRATEGY.md](STRATEGY.md). This document records a
backtested specification only.

Developed on Nifty 100 daily bars (Kite historical) over
**16 Oct 2024 → 16 Oct 2026**, then tested on **16 Oct 2022 → 16 Oct 2024**
out of sample. The out-of-sample result changed the averaging rule and
rejected a turnover filter — see "Out-of-sample test" before using any
in-sample figure.

---

## Capital and sizing

| Item | Value |
|---|---|
| Wallet | ₹7,00,000 |
| MTF leverage | 4.4x |
| Exposure per leg | ₹50,000 |
| Own capital per leg | ₹11,364 |
| Max averages per position | **1** (so max ₹1,00,000 exposure / ₹22,727 own per stock) |
| Funding cost | 14%/yr on the funded portion, exposure × (1 − 1/4.4) |

Own capital tied up averaged ₹1,38,924 over the test and peaked at
**₹5,56,818**. The wallet must stay free — signals cluster, see "Risks".

**On the ₹7,00,000 figure.** The study was run at ₹10,00,000 and then re-run on
smaller wallets. ₹7L produces an *identical* net P&L and an identical 104
trades with zero blocked entries, so ₹3L was dead weight; the return rises
from 43% to 62% without changing a rule. ₹6L blocks one entry, ₹5L blocks five
and costs ₹66,923. Run **₹7.5–8L live**: the backtest fills at the open with no
slippage or partial fills, so keep a buffer against a blocked entry during a
signal cluster.

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

A ₹100 crore/day turnover floor was tried and **rejected**: it gained ₹31,260
in-sample but cost ₹71,264 in the out-of-sample year (−₹11,403 → −₹82,667).

## Averaging

Add **one** more ₹50,000 leg if price trades **3% below the entry price**. That
is the only add. Same rupee size — never doubled.

**This was 3 averages until out-of-sample testing.** Deeper laddering is what
breaks the strategy outside its development window — see "Out-of-sample test".
Uncapped averaging drove the test account to negative equity (−₹27,672 and
−₹2,32,981 in separate runs).

For a lower-risk variant, run **no averaging at all**: 4-year P&L +₹1,61,625
at a 13.1% worst drawdown, versus +₹2,86,994 at 23.1% for max 1. The two are
indistinguishable risk-adjusted (₹12,338 vs ₹12,424 of profit per drawdown
point), so the choice is purely how much drawdown is acceptable.

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

₹7,00,000 wallet, position size fixed throughout (no compounding).

Figures below are for the ORIGINAL max-3 configuration over the two
development years, retained for reference. **Read the out-of-sample section
before relying on any of them.**

| | |
|---|---|
| Net P&L | **+₹4,30,528** on ₹7L (+61.5% over 2 years, ~30.8%/yr) |
| Gross P&L | +₹5,52,421 |
| Charges | −₹30,501 |
| MTF interest | −₹91,391 |
| Trades | 104 (215 buy legs) |
| Win rate | 56.7% |
| Average win / loss | +₹11,124 / −₹5,017 |
| Median holding period | 34 days |
| Max drawdown | 21.8% / 22.8% by half on ₹7L (≈₹1.55L; was 15.9% on ₹10L) |
| Lowest equity | ₹6,31,486 (H1) / ₹5,72,590 (H2) on ₹7L |
| Year 1 / Year 2 | +₹2,24,122 / +₹2,06,406 |
| Return on capital actually deployed | 310% |
| Entries blocked for lack of funds at ₹7L | 0 |

Interest is **75% of total cost**. The only levers on it are shorter holds
and less borrowing.

## Out-of-sample test

Data was later extended back to May 2021, giving two years the rules were never
tuned on. Each year is an independent run from a fresh ₹7,00,000.

| year | | max 3 (original) | max 1 | no averaging |
|---|---|---|---|---|
| Y1 2022-10 → 2023-10 | **out of sample** | **−11,403** (DD **43.7%**) | +9,525 (23.1%) | **+20,445** (13.1%) |
| Y2 2023-10 → 2024-10 | **out of sample** | −7,245 (2.4%) | −6,677 (2.0%) | −4,842 (1.1%) |
| Y3 2024-10 → 2025-10 | in sample | +224,122 (21.8%) | +172,293 (17.4%) | +92,437 (12.1%) |
| Y4 2025-10 → 2026-10 | in sample | +206,406 (22.8%) | +111,853 (18.7%) | +53,585 (12.4%) |
| **4-year sum** | | +411,880 | **+286,994** | +161,625 |
| **years positive** | | **2/4** | **3/4** | **3/4** |

**The max-3 configuration is 0 for 2 out of sample.** All of its profit comes
from the two years it was developed on. Reducing the ladder to one add recovers
the out-of-sample year and cuts worst-case drawdown from 43.7% to 23.1%; removing
it entirely cuts drawdown to 13.1%.

**Why Y1 broke it.** Y1 contains the Adani short-seller collapse — ADANIGREEN
fell 58% between 1 Dec 2022 and 31 Mar 2023 and touched −78%. The strategy
bought its 52-week low and averaged into it three times, for −₹62,575 on that
name alone. ADANIENT is the biggest in-sample winner and ADANIGREEN the biggest
out-of-sample loser: same group, same mechanism, opposite outcome.

**Y2 is absence, not failure.** Two trades in twelve months. 2023-24 was a
sustained bull market and almost nothing printed a 52-week low. The strategy
cannot be relied on for income in a rising market.

**Survivorship bias cuts against these numbers, not for them.** `data/nifty100.csv`
is today's index membership; applied to 2022-23 it omits demoted and delisted
names and includes later promotions early. The true out-of-sample result is
likely worse than shown.

## Risks

0. **Only 3 of 4 years are positive even at max 1 average, and the two
   strongest years are the development window.** Treat the in-sample returns as
   an upper bound, not an expectation.

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
| Holding-period limit (exit if unarmed after 45/60/90/120/180d) | Noise, non-monotonic: −₹24,480 to +₹5,222. 45d loses, 120d gains, 180d loses |
| Hard holding cap (60/90/120/180d) | Same: −₹9,124 to +₹10,792. The best-looking cap (120d) lands just inside the 122-day ADANIENT winner — fitting, not signal |
| Interest cap as a cost stop (3/5/7% of exposure) | +₹1,853 at best, nil at 5% and 7% |
| Additional ATR% ≥ 2.0 or 2.5 condition | Exactly zero change — redundant with the range% filter |
| Dropping the Information Technology sector | −₹25,659 in H1 against +₹29,054 in H2; opposite signs, so fitting |
| Second always-in sleeve on the idle capital (top-10 volatile, target BE+8%) | More money (₹5,98,875) at 51.2% drawdown — 2.2x worse per drawdown point, and the sleeve flips sign with its capital allocation |
| Always-in + fixed profit target, any size | Top-25 variants reached **negative equity** (−₹1,90,721) at 111–119% drawdown; 93.6% win rate with average win ₹2,102 against average loss ₹33,358 |

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
- Do **not** add a maximum holding period. Positions ran to 242 days (TCS,
  ₹9,262 of interest, −₹21,013) but cutting slow positions also cuts the ones
  that recover, and every time limit tested came out as noise.
- This strategy shares the Kite API key with the live intraday bot. Refreshing
  the access token invalidates the live session, so re-auth only after
  15:30 IST on a trading day.
