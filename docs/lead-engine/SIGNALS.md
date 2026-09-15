# Signal states

Ten states, ordered by how much is being claimed.

| State | Means |
|---|---|
| `IDLE` | nothing worth naming |
| `WATCH` | pressure ≥ 35 on one side, or both sides fighting |
| `PRE_SIGNAL` | pressure ≥ 55, no level under stress yet |
| `PRE_BREAK_LONG` / `PRE_BREAK_SHORT` | break probability ≥ 60 at a named level |
| `HIGH_PROBABILITY` | break probability ≥ 72 |
| `A_PLUS` | break probability ≥ 85 **and** pressure ≥ 55 |
| `REVERSAL_CANDIDATE` | liquidations exhausted and pressure has turned the other way |
| `INVALIDATED` | a setup that was building has gone |
| `DATA_FAILURE` | the feed is not good enough to have an opinion |

Thresholds are configurable (`T3_LEAD_ENGINE_PREBREAK_PROBABILITY`,
`..._HIGH_PROBABILITY`, `..._A_PLUS_PROBABILITY`).

## Two rules that shape everything

**`DATA_FAILURE` wins over everything.** A degraded feed does not produce
a cautious signal, it produces none. Every number feeding the machine is
suspect at exactly the moment it would be most tempting to act on one, and
a signal computed from stale data is not smaller, it is wrong. The health
monitor decides this, not the scores.

**`INVALIDATED` is a state, not a return to `IDLE`.** A setup that was
building and then broke down is a different thing from a market that was
never interesting, and collapsing the two erases the only record that the
engine was wrong. It holds for 60 seconds, then decays.

## Direction

From the pre-break probabilities when either is saying anything; from
pressure when both are silent. Deciding it on the probabilities alone
described a market with 60 short pressure and no level nearby as "long",
because zero is not less than zero. There is a test.

## Timestamps

When the state does not change, the numbers refresh but `changed_at` does
**not** move. "In `PRE_BREAK_SHORT` for 40 seconds" is a fact about the
setup, and refreshing the timestamp every tick would erase it.

## Where they go

The Lead Engine's own bus (`lead_engine.signal`), its own API, its own
tab, and `lead_engine_signals` in Supabase. Nothing reaches the project's
signal engine, risk engine or execution path — see ARCHITECTURE.md.
