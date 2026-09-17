# What 28.7 hours of recorded output says about the thresholds

Written after a request to tune the engine until it trades. It does not
end in a tuning, and the reason is the point of the document.

## The question

`prebreak_probability = 60.0` is the score at which PRE_BREAK fires. In
28.7 hours of live recording across seven instruments the engine crossed
it **once**. Should it be lowered?

## The data

`lead_engine_features`, 2026-09-15 12:38 → 2026-09-16 17:20, 19,769 rows
over seven symbols. Every row carries the price and the engine's own
break scores, so "what happened next" is answerable directly.

## First cut, and why it was wrong

Bucketing by score and measuring the forward five-minute return in the
direction the engine leaned gave a clean-looking monotonic result: 45%
hit rate below 40, 58.5% at 40-50, 72.7% at 50-60.

That was confounded twice over. When no level is found both scores are
zero and the "direction" defaults to long, so the lowest bucket was
measuring market drift, not the engine. And the market fell over the
window - **baseline buy-and-hold was -1.49 bps at 45.1%** - which
flatters every short call regardless of why it was made.

## Second cut, controlled

Split by side, compared against each side's own baseline:

| | n | avg bps | win % |
| --- | --- | --- | --- |
| baseline (buy always) | 18,338 | -1.49 | 45.1 |
| LONG score ≥ 40 | 300 | -0.77 | 42.7 |
| LONG score ≥ 45 | 83 | +2.03 | 53.0 |
| SHORT score ≥ 40 | 423 | +5.27 | 70.9 |
| SHORT score ≥ 45 | 108 | +7.12 | 77.8 |

Both sides beat their baseline above ~45, the short side strongly. That
looks like a reason to lower the threshold to 45.

## Third cut: costs

A round trip is 2 × 5.5 bps taker + 2 × 1 bp slippage = **13 bps**. The
signal's median favourable excursion inside a 15-minute hold is **11.7
bps** (mean 21.8, MAE median 10.7). The cost is larger than the move the
signal typically predicts.

A path-dependent simulation over seven target/stop pairs - first touch
wins, unfilled exits at the 15-minute price - found **every combination
negative**, the best being target 30 / stop 15 at **-4.47 bps per trade**
over 367 observations.

## Fourth cut: the horizon trap

Extending the hold appeared to rescue it. At score ≥ 40 the short side
returned +43.30 bps gross at 30 minutes (73.9% win) - and **-24.49 bps at
60 minutes, 12.8% win**.

A signal that is +43 bps at half an hour and -24 bps at an hour is not a
signal. It is one market move sliced by the clock. A real edge decays; it
does not invert.

## Fifth cut: independence

Those "423 trades" are snapshots taken every ~15 seconds. Grouping
consecutive signals into episodes (a gap over 10 minutes starts a new
one) gives **83 episodes, 5.4 rows each**, and taking one observation per
episode collapses the result:

| | episodes | gross bps | win % |
| --- | --- | --- | --- |
| 30-minute hold | 45 | +11.2 | 53.3 |
| 60-minute hold | 45 | -13.5 | 29.6 |

45 independent episodes, spread over **10 distinct hours** of the 28.7.
At 13 bps of cost the 30-minute figure is **-1.8 bps net**, on a coin
flip's win rate.

## Conclusion

**The thresholds were not changed.** The recorded data cannot support it:
one regime, one direction, 45 independent episodes clustered into ten
hours, and an apparent edge that inverts when the horizon moves. Lowering
the threshold on this evidence would manufacture trades whose expected
value is negative after costs - which is worse than the engine not
trading, because it would look like progress.

What the data does establish:

1. The score is not noise. Above ~45 it separates from baseline on both
   sides, consistently enough to be worth accumulating.
2. **Costs, not the threshold, are the binding constraint.** A signal
   whose median excursion is 11.7 bps cannot pay 13 bps to trade. Either
   the edge has to be larger or the execution cheaper; no threshold fixes
   that arithmetic.
3. The 60.0 threshold is nonetheless miscalibrated for *observation*: it
   admits one case a day, so nothing accumulates.

## What to do instead

`calibration.py` already exists for exactly this. It opens an observation
whenever a level is under stress - **independently of the signal
threshold** - and converts a score into an empirical probability once 30
cases in that bucket have resolved. It needs regimes, not tuning: a
rising market, a falling one, and a quiet one.

The honest sequence is to leave the threshold where it is, let the
calibrator fill, and revisit when `calibration.summary()` reports
resolved buckets rather than `not calibrated`. The virtual ledger will
then have something to measure that is not one afternoon's move.
