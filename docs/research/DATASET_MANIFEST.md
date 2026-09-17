# DATASET_MANIFEST

Two datasets exist in this project. They are different KINDS of thing and
the difference decides what each may be used to claim.

---

## 1. `tests/fixtures/bybit_capture_injusdt.jsonl.gz` — SYNTHETIC

| | |
|---|---|
| classification | **SYNTHETIC** (a hand-built scenario) |
| described in the repo as | "twenty minutes of six Bybit topics ... saved rather than generated" |
| that description is | **wrong** |
| period | 1700000000000 → 1700001199500 (0.333 h) |
| frames | 10,288 |
| sha256 | `8d7c47feb58045f84170e9236b54213b15ad5d39a020d9c5f095b054a3973a2a` |

Three independent tells, any one of which is sufficient:

* **A perfect grid.** Every timestamp is a multiple of 500 ms. Real
  exchange stamps are irregular.
* **A typed start.** It begins at exactly `1700000000000`, and the span
  is exactly 1199.5 s.
* **No size entropy.** Every BTCUSDT level holds exactly `8`.

It also reports 2.0 book updates/s and a median of **80 levels per
update**, against 14.4/s and **6 levels** in the real recording. It does
not look like a market.

**What it may be used for**: a deterministic regression test of the
ingest pipeline. The same bytes give the same report, so a change in the
report is a change in the engine. That is genuinely valuable and it stays.

**What it may not be used for**: any claim about how a market behaves,
any claim about what an order would have paid, any execution result. The
previous round's report cited a "replay of 10,288 recorded Bybit frames"
as evidence of engine correctness. The replay was real and the engine
did run clean; the word "recorded" was not.

---

## 2. `research_captures` (Supabase) — REAL_CAPTURE

| | |
|---|---|
| classification | **REAL_CAPTURE** (no warnings raised) |
| source | Bybit public linear websocket, `wss://stream.bybit.com/v5/public/linear` |
| recorder | `t3_engine.research.capture`, running on Render |
| market | Bybit USDT perpetual (linear) |
| symbols | INJUSDT (DOGEUSDT for the first 20 minutes) |
| topics | `orderbook.50`, `publicTrade`, `tickers`, `allLiquidation` |
| dropped by the recorder | **0** |
| receive timestamps present | **100%** |

First measured window (24 segments, 22,383 frames, 0.338 h):

| measure | value |
|---|---|
| median inter-frame gap | **35 ms** |
| p95 inter-frame gap | 113 ms |
| max gap | 1,505 ms |
| gaps over 5 s | 0 |
| book updates / s | 14.39 |
| trades / s | 1.55 |
| median levels per update | 6 |

### What was recorded, exactly

* **Order book**: Bybit's own snapshot and deltas. Deltas are **merged
  into exact 100 ms windows**. Merging is lossless at the boundary - a
  delta states the size a price now holds, so the last word in a window
  is the whole truth about that window's end - and a test reconstructs
  the book from the merged stream and from every frame and asserts the
  two are identical. What is lost is resolution *between* boundaries.
* **Trades**: every one, with aggressor side, price, size and both
  timestamps. Never coalesced.
* **Liquidations**: every one. Never coalesced.
* **Tickers**: thinned to one per 5 s. They were 31% of frames and over
  half the bytes, for a 1 Hz republication of a funding rate that changes
  every eight hours.

### Why 100 ms and not finer

A decision on this deployment reaches the exchange in roughly 150–250 ms
(send + ack, measured conservatively). Book detail finer than that cannot
inform a tradeable decision here, so recording it would spend the quota
on resolution no strategy on this hosting could use. Anything claiming an
edge inside 100 ms is out of scope for Render Free, and this dataset says
so rather than implying otherwise.

### What this data cannot support

**Queue position.** Bybit's public book is aggregated per price level.
124.64 contracts at 5.759 might be one order or forty, and nothing here
can say where ours would sit. Every maker fill in every result is the
simulator's ESTIMATE under an explicit queue model, reported under both a
conservative and an optimistic reading. A result that exists only under
the optimistic one has not been shown to work.

**Market reaction to our own orders.** Replay cannot show how the book
would have answered. Sizes are capped against observed liquidity and the
same resting size is never handed to two of our own orders, but a large
order's real impact is outside what any recording can support.

### Integrity

Each segment stores a sha256 **over the raw decompressed frames**, so the
hash still means something to anyone holding the decoded file rather than
this encoding. `verify_segment()` checks the hash and the frame count and
a corrupt segment is skipped loudly rather than quietly repaired.
`segment_id` is the idempotency key, so a retry after a write timeout
upserts rather than double-billing the byte budget.

### Reproducing

```sql
-- what exists
select count(*) segments, sum(frames) frames, sum(dropped_before) dropped,
       round(sum(stored_bytes)/1e6,3) stored_mb,
       min(to_timestamp(first_exchange_ms/1000.0) at time zone 'UTC') from_utc,
       max(to_timestamp(last_exchange_ms/1000.0) at time zone 'UTC') to_utc
from research_captures;

-- re-measure the manifest
insert into research_jobs (job_id, kind, spec, status)
values ('manifest-inj-002', 'manifest',
        '{"symbol":"INJUSDT","max_frames":200000}'::jsonb, 'pending');
```

```bash
# the fixture's manifest, locally
python3 -c "
import gzip, json
from t3_engine.research.dataset import describe
frames=[json.loads(l) for l in gzip.open('tests/fixtures/bybit_capture_injusdt.jsonl.gz','rt') if l.strip()]
for f in frames: f.setdefault('r', f.get('ts'))
print(describe(frames, name='fixture', source='repo', provenance='commit 661b036').render())"
```
