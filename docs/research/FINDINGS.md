# Findings

Every number here comes from `research_jobs` / `research_experiments` and
can be re-derived with the SQL at the end. Sample sizes are stated
because they are small and the conclusions are drawn accordingly.

---

## 1. Hypothesis B — passive spread capture — is REFUSED

Three independent lines, and they agree.

### The arithmetic, before any data

Bybit charges a non-VIP maker **2 bps per side**, so two maker fills cost
**4 bps**. The spread must clear that before a single adverse fill.
Pre-registered in `COST_MODEL.md`, and `SpreadCapture.min_spread_bps()`
is derived from the fee schedule rather than tuned.

### The measurement

| instrument | spread median | p95 | max | below the 4 bps floor |
|---|---|---|---|---|
| INJUSDT | **1.81 bps** | 3.61 | 5.43 | **99.64%** |
| ATOMUSDT | **6.53 bps** | 6.53 | 13.05 | **0.0%** |

INJUSDT cannot pay, at any parameterisation. ATOMUSDT always can — which
is why it was added to the recording, as a real test rather than an
argument.

Measured adverse selection — where the mid goes after an aggressive
trade, which is what a resting order on that side paid:

| instrument | @1 s | @5 s | @30 s |
|---|---|---|---|
| INJUSDT | 3.61 bps | 5.40 | 4.52 |
| ATOMUSDT | 6.53 bps | 6.53 | 6.53 |

ATOMUSDT gives back **almost exactly its spread**. INJUSDT gives back two
to three times it. The model's original assumption of half a spread was
optimistic by a factor of two to six, and the default is now 1.0.

**The consequence reverses the earlier conclusion.** Under the assumed
figure, maker/maker break-even was FLAT at 4 bps at every spread. Under
the measured figure it **rises with the spread**: 5.81 bps at INJUSDT's,
10.53 at ATOMUSDT's, 24.00 at twenty. A wider spread earns more and gives
back more than it earns.

### The simulation, on the instrument that could have worked

`bt-atom-spread-r1`, ATOMUSDT, one 20-minute window:

| | |
|---|---|
| orders placed | 281 |
| orders cancelled | 276 |
| **fill rate** | **2.14%** |
| maker fills / taker fills | 5 / 3 |
| round trips | 3 |
| **win rate** | **0.0%** |
| gross / fees / **net** | −0.531 / 0.306 / **−0.837** |
| **expectancy** | **−20.53 bps per trade** |
| maker entries / **maker exits** | 3 / **0** |
| exits | 1 STOP, 2 TIME_STOP, **0 at target** |
| filled while cancelling | 1 |

Four failure modes, all of them the ones the simulator exists to expose:

1. **Quoting both sides is not capturing the spread.** 281 quotes, 5
   maker fills. One side fills and the other does not.
2. **Having been filled, you cannot leave passively.** `maker_exits = 0`
   — every position was closed by crossing, so the realised regime was
   maker/taker at 7.5 bps, not maker/maker at 4.
3. **Adverse selection is real and it is the measured 6.53 bps.** Zero
   wins out of three; no exit reached its target.
4. **The cancel is not free.** One order filled while its cancel was in
   flight — the loss a strategy really takes when it tries to pull a
   quote into a move.

**Sample: 3 trades, 1 episode, one window.** That is not statistical
proof and the bootstrap correctly refuses to give an interval (n=1). The
conclusion is carried by the arithmetic and the measurement; the
simulation is a mechanism demonstration that agrees with them.

---

## 2. Hypotheses A and C — NO_TRADE, with the binding constraint named

Neither traded on the replayable windows. That is a result, and the
refusal counts say which condition is binding.

**A — order-flow impulse, INJUSDT (6,170 events)**

| reason | count |
|---|---|
| volatility too low to pay for a round trip | **5,954** |
| not warm | 28 |

One constraint, binding on essentially every frame. INJUSDT's realised
volatility over the window was below the floor at which the expected
move (volatility × 1.5) could clear a 12.81 bps break-even. **The
strategy refused a dead tape, which is what it was built to do.** It says
nothing yet about whether the mechanism works when the tape is alive.

**C — sweep reversion, INJUSDT (6,226 events)**

| reason | count |
|---|---|
| liquidity has not come back | **3,677** |
| no sweep to fade | 1,966 |
| sweep recorded; waiting for exhaustion | **159** |
| expected retrace does not clear the round trip | **116** |
| no reclaim yet | 63 |
| the move is still extending | 15 |

Sweeps were detected 159 times. The conjunction then failed: liquidity
did not return. 116 times the trigger fired and the expected retrace
still did not clear costs. This is the "do not buy every dip" rule doing
its work — and it is the most promising of the three, because the
mechanism fires and only the follow-through is missing.

**C — sweep reversion, ATOMUSDT**: 38 sweeps recorded, 693 refusals on
liquidity, 86 on spread too wide, 8 on cost.

---

## 3. What is still unknown

* Whether A's mechanism works on a live tape. The volatility filter has
  never been cleared, so it has not been tested — only gated.
* Whether C's reclaim ever completes. The trigger fires; the sequel has
  not been observed.
* Anything at all out of sample. There is not yet enough capture for a
  train / validation / untouched final test across days and regimes.

**No candidate is confirmed. One is refused on measured evidence.**

---

## Reproducing

```sql
select job_id, status,
       result->'report'->>'trades', result->'report'->>'net_pnl',
       result->'report'->>'expectancy_bps',
       result->'report'->>'strategy_declines',
       result->'assessment'->>'verdict'
from research_jobs where job_id like 'bt-%' order by job_id;

select job_id, result->'spread_bps', result->'adverse_selection_bps',
       result->>'spread_below_maker_fee_floor_pct'
from research_jobs where kind = 'microstructure' order by job_id;
```
