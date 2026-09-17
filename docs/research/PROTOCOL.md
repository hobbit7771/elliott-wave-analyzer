# Pre-registration

Written and committed BEFORE any strategy was run against the recording.
The git history of this file is the evidence for that claim, and a change
to it after a final-test result has been seen invalidates that result
rather than updating it.

## Hypothesis families, and the mechanism each claims

Three, no more. A fourth family invented after seeing a result is a new
experiment needing a new untouched period.

**A — directional order-flow impulse.** Aggressors hit one side; the
resting depth there drains and does not replenish; with too little left
to absorb the arriving flow, the price must move to find liquidity. The
loser is a resting limit order whose owner has not yet observed the flow
picking it off.

**B — passive spread capture.** Whoever needs immediacy pays for it. A
resting two-sided quote sells immediacy and collects the spread. The
loser is the impatient taker.

**C — sweep / exhaustion / absorption reversion.** A burst of one-sided
aggression that makes no further progress, after which liquidity returns
and the price reclaims the level, is a FORCED seller rather than an
informed one. Forced flow carries no information about value, so the
price it printed is not a price anyone chose. The loser is the forced
seller, who is not choosing the price at all.

## Parameters: frozen, and where the ranges may go

Defaults are in `strategies.py` and are frozen at the commit that
introduced them. The live PAPER traders were started on those exact
defaults before any backtest existed, so the forward record is out of
sample with respect to anything fitted later.

| family | may be searched | may NOT be searched |
|---|---|---|
| A | taker delta 0.40–0.70, depth drain 0.20–0.50, replenishment 0.10–0.35, confirmation 0.0–2.0 bps, hold 30–180 s | the cost gate |
| B | TTL 500–5000 ms, inventory limit, skew, toxicity filters | **`min_spread_bps`** — it is DERIVED from the fee schedule |
| C | burst intensity 2–8/s, burst delta 0.5–0.9, sweep 4–12 bps, reclaim 0.5–3 bps | the requirement that all three of trigger, exhaustion and reclaim hold |

**Budget: 60 backtest runs in total across all families**, ablations and
stress scenarios included. The count is kept by `research_experiments`,
which holds every run whether or not it worked. When the budget is spent
it is spent; continuing to search is how a threshold gets tuned until
noise looks like an edge.

## The target metric

**Net expectancy in bps per episode, after all modelled costs, subject to
the risk limits.** Not win rate, not trade count, not gross P&L. A
strategy that wins 80% of the time and loses money is a strategy that
loses money.

Preference is given to a plateau over a peak: a parameter point whose
neighbours also work is a finding, and one that works alone is a fit.

## Risk limits, fixed here

| limit | value |
|---|---|
| capital | 10,000 USDT (notional, paper) |
| max position notional | 2,000 |
| max per symbol | 1,000 |
| max total | 3,000 |
| max concurrent positions | 3 |
| max correlated-group notional | 1,500 |
| max daily loss | 200 |
| max drawdown | 500 |

No martingale, no averaging down, no leverage increase and no size
increase to rescue a negative expectation. Every comparison between
strategies is at these same limits.

## Splits

Chronological train / validation / final test, **purged** by at least the
longest hold any strategy can take, so a position opened at the end of
one split cannot resolve inside the next.

The final test is spent ONCE. A set looked at twice is not untouched, and
the second look is where the flattering number comes from. Changing a
hypothesis after seeing it requires a new period.

## Acceptance

All of these, or the verdict is not CONFIRMED:

* net P&L > 0 and expectancy > 0 on the untouched period
* profit factor ≥ 1.2
* ≥ 30 independent EPISODES over ≥ 3 distinct days
* no single day is more than 60% of the profit
* no single symbol is more than 80% of the profit
* the 95% block-bootstrap interval over episodes excludes zero
* it survives the stress scenarios: latency ×2 and ×4, fees +50%,
  participation capped at 10%, and it must not DEPEND on the optimistic
  queue model

If the interval includes zero the status is **PRELIMINARY_CANDIDATE**,
never confirmed. If nothing clears the bar the answer is **no edge
found**, and that is a result to be reported, not a reason to keep
searching.

## Independence

An episode, not a row, is the unit. Repeated snapshots of one setup are
one observation; overlapping positions are one observation; simultaneous
signals on correlated instruments are one observation. Uncertainty comes
from resampling contiguous time blocks, never from shuffling individual
trades, because shuffling assumes the independence this data does not
have.

The previous round of this project reported 451 signal rows as 451
observations. They were 83 episodes, and 45 after one observation per
episode. That is the mistake this section exists to prevent.

## Horizon

Where a result changes SIGN between horizons, that is a finding to be
investigated - the dependence itself is the thing to measure - and not an
automatic conclusion that there is no signal. The previous round observed
+43.30 bps at 30 minutes and −24.49 bps at 60 minutes on the same calls
and stopped there. The horizon sweep is part of the analysis, not a
tie-break.
