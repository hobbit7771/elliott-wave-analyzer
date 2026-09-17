# Defect map

Each item is the one raised in the brief, what was actually found, and
what pins it. "Verified" without a fix means the defect was not present
in the deployed code and a regression test now says so.

| # | Defect | Status | Evidence |
|---|--------|--------|----------|
| 1 | Backfill must insert by timestamp, merge overlaps and duplicates, never let a forming update replace a closed bar, and give the same series on a warm start, a live-before-history start and a repeated backfill | **Two real bugs, both fixed** | `smc_engine.py`; 6 tests in `test_lead_engine_pipeline.py` |
| 2 | New historical context becomes available at receipt; no signal is moved back in time | **Verified** | Structure is fed on arrival and nothing rewrites a past state; `test_the_levels_are_the_same_whichever_order_the_bars_arrived_in` |
| 3 | The rate-limiter test must use a clock it controls | **Fixed** | `test_lead_engine_external.py`; 4 tests, mutation-checked against a deliberately broken limiter |
| 4 | A zero directional score with no signal must be NONE, not LONG | **Verified; the bug was in the analysis, not the engine** | `test_a_zero_score_never_names_a_side` |
| 5 | Repeated snapshots of one signal are not separate trades or independent observations | **Fixed** | `backtest.episodes()`, `episode_returns()`, episode ids on every journal row |
| 6 | Replay needs an injectable clock and must reproduce data availability | **Fixed by the new path** | `runner.py` reads no wall clock; `test_an_order_cannot_fill_before_it_could_have_arrived` |
| 7 | Positions, journal and calibration must survive a restart | **Journal done earlier; calibration was never persisted at all - fixed** | `calibration.py`, `storage.py`; 6 tests |
| 8 | Stale book, lost trades, resync, backlog and disconnect must block new entries | **Fixed** | `runner._assess`; 3 tests |

## 1. The backfill merge

Two distinct bugs, one found in each round.

**Out-of-order insertion.** `SmcEngine.update` appended any candle that
was not the newest, leaving the series non-monotonic. `find_swings` is
purely positional - it compares each bar with its neighbours BY INDEX and
never reads a timestamp - so the same bars in a different order give a
different swing set, different levels and a different break score. The
REST backfill made it reachable: 240 historical minutes are seeded on
their own thread while the kline socket is already running, so on a warm
start the live bars arrive first. Measured on 240 bars plus twelve warm
minutes: **nearest support 98.27 in order, 98.13 out of order.**

**A closed bar replaced by a forming one.** Replacement was
unconditional, so a live update for a minute the backfill had already
delivered CLOSED overwrote the finished bar - the minute's true high and
low replaced by whatever had printed in the fraction the socket saw.
`_supersedes` now refuses exactly that one case and permits every other.

Together they give the property the brief asks for, and it is asserted
directly: cold start, live-before-history and a repeated backfill produce
**byte-identical series and identical levels**.

## 4. The LONG default

There is no long-biased default anywhere in the engine. `SignalMachine`
returns no direction at a zero score, and nothing downstream reads the
direction as truthy-or-long.

The defect was real, but it was in the **analysis script** used for the
previous calibration, which derived a side as `long if long_score >=
short_score`. At a 0-0 tie that scores every directionless moment as a
long call, and since the market fell over the window it made the lowest
score bucket look like a losing long strategy. That confound is written
up in `CALIBRATION_FINDINGS.md`, and it is the reason the first cut of
that analysis looked monotonic when it was not.

The research package avoids the shape entirely: a side is only ever set
by an explicit condition, never by a comparison that has to break a tie.

## 7. Calibration was never persisted

The one genuinely new finding in this group. `calibration.py` held every
observation in memory and had no read or write path at all.

Why that matters more than it looks: the calibrator needs **thirty
settled observations in a bucket** before it will report a probability,
and it opens a handful an hour. Render's free plan stops a web service
about fifteen minutes after the last inbound request. So the record was
discarded several times a day, and `summary()` would have reported
`not calibrated` **forever**, however long the engine ran - which is
exactly what it had been reporting.

Settled observations are now written keyed on
`symbol:direction:opened_ms` and reloaded once per symbol at startup.
PENDING observations are deliberately NOT stored: one belongs to a
process that no longer exists, and settling it would mean measuring an
outcome against prices nobody was watching.
