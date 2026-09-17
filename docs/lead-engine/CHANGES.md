# What was added, what was touched, and where the line is

## Files added

**The engine** — `t3_engine/lead_engine/` (22 modules):
`__init__.py`, `config.py`, `bus.py`, `rolling.py`, `bybit_ws.py`,
`orderbook_engine.py`, `microprice.py`, `trade_flow.py`, `cvd.py`,
`candles.py`, `liquidation_engine.py`, `oi_engine.py`, `btc_leadlag.py`,
`smc_engine.py`, `elliott_state.py`, `prebreak_engine.py`,
`pressure_engine.py`, `signal_machine.py`, `health.py`, `state.py`,
`engine.py`, `storage.py`, `replay.py`, `api.py`, `requirements.txt`.

**The tab** — `t3_engine/dashboard/static/lead_engine.js`,
`t3_engine/dashboard/static/lead_engine.css`.

**Tests** — `tests/test_lead_engine.py`,
`tests/test_lead_engine_api.py`, `tests/test_lead_engine_isolation.py`.

**Docs** — this folder.

## Existing files changed — two, minimally

### `t3_engine/dashboard/server.py`

1. Three imports (`lead_engine.api`, `lead_engine.config`,
   `lead_engine.engine.get_engine`) and one `app.include_router(...)`.
2. A `startup` hook that calls `LeadEngine.start()` inside a `try`, and a
   `shutdown` hook that stops it.
3. The build version string.

Nothing else. No existing handler, model or response shape was altered. A
test asserts that `server.py` imports only the Lead Engine's facade,
config and router — never a feature module.

### `t3_engine/dashboard/static/index.html`

1. A tab button.
2. An **empty** `#tab-lead` panel.
3. A `<link>` and a `<script>`.
4. One entry in `TAB_PANELS` and one `show()`/`hide()` pair in the tab
   switcher, plus hiding the shared chart on this tab.

A test asserts that no Lead Engine rendering code leaked into the shared
script.

## What was NOT touched

`elliott_engine/`, `signal_engine/`, `risk_engine/`, `execution/`,
`position_manager/`, `pipeline/`, `backtest/`, `ai_advisor/`,
`market_structure/`, `orderflow/`, `derivatives/`, `fibonacci/`,
`candle_builder/`, `common/`, `config/`, `database/models.py`, and every
existing table. No strategy, no threshold and no wave rule was changed.

## The permitted interfaces

**Analyser → Lead Engine**, only these:

```python
LeadEngine.subscribe(symbol)
LeadEngine.get_state(symbol)
LeadEngine.get_signal(symbol)
LeadEngine.get_pressure(symbol)
LeadEngine.status()
```

plus `lead_engine.config.enabled()` and the router object. All return
plain dictionaries; no object from inside the package escapes it.

**Lead Engine → Analyser**, only this:

```python
t3_engine.database.supabase_rest        # shared PostgREST transport
```

used exclusively for the `lead_engine_*` tables, with `storage.py`
refusing any other table name at runtime.

One further interface is *available and unused by default*:
`ElliottContext(reader=...)` accepts a callable that returns the project's
own wave count as read-only context. Nothing wires it up; the engine runs
its own deterministic state machine instead. If it is ever connected, it
reads and never writes, and a failing reader is treated as "no context".

## Bugs found while building this

Recorded because each one is now a test:

* `aggregate_candles` in the analyser — pre-existing, fixed earlier.
* **Crossed book undetected.** Contiguous update ids, a book quoting 5.90
  bid against a 5.65 ask, and a well-formed imbalance computed from it.
  The sequence check cannot see this. Now checked directly.
* **`EXHAUSTION` unreachable.** Measured against the minute's average
  instead of the peak 5s rate; a four-second flush averaged over sixty
  seconds fell below every threshold.
* **`fading_bounces` always zero.** A bounce was recorded for every
  consecutive bar sitting at the level, each measuring zero.
* **Levels never found.** Derived from 1-minute bars, which give ten bars
  in a ten-minute capture — too few for a fractal swing. Now 15-second
  bars built from trades.
* **Direction reported as "long" on a short market.** Chosen from the
  pre-break probabilities alone, and zero is not less than zero.
* **Nothing persisted unless a browser asked.** `api.state()` filed a
  feature row and nothing else wrote at all, so a signal that fired
  overnight left no trace and `lead_engine_liquidations` could never fill.
  Found after the first live deploy, by looking for rows that were not
  there. `storage.Recorder` now writes on the engine's own clock.

---

# Round 2 — signal logic, stable UI, professional chart

## What was wrong, and what it is now

| Was | Now |
| --- | --- |
| `delta_ratio = buy / sell` ran to a million on a one-sided tape | `normalized_delta = (buy − sell) / (buy + sell + ε)`, bounded on [−1, +1] (`normalize.py`) |
| Raw, unbounded features fed straight into the score | Every feature normalised first — rolling z-score clipped at ±3σ, percentile for fat tails, or ratio-to-median. 600-sample windows, 8-sample minimum before a feature counts at all |
| OBI at level 1 alone could produce a strong signal | `top_book_score` (levels 1–5) and `deep_book_score` (10–50) are separate, and `BOOK_ALIGNMENT = (0.35·top + 0.65·deep) · consistency`. A bullish top book over a bearish deep book is labelled `TOP_BOOK_BULLISH_DEEP_BOOK_NEUTRAL`, not "bullish" — measured: the book component for OBI1 = +0.98 against a bearish deep book fell from 0.53 to 0.175 |
| Walls were a size threshold and nothing more | Classified per wall: `TRANSIENT`, `PERSISTENT`, `REPLENISHING`, `POSSIBLE_SPOOF`, `ABSORBED`, each with a weight. Executed volume is attributed to the wall at that price, and whether price actually *reached* a wall is latched rather than inferred from best bid |
| Absorption was an unbounded number | Scored 0–1 three ways — against volume, against depth, and as a rolling percentile |
| One weighted blob called "pressure" | Five independent layers, each scored on its own evidence with its own confidence and its own `missing` list: FLOW 30%, BOOK 25%, STRUCTURE 20%, DERIVATIVES 15%, BTC_LEAD 10% — all in `config.py`, none hard-coded in the scorer |
| Layers disagreeing was invisible | `CONFLICT_HIGH` / `MEDIUM` / `LOW` from cross-layer agreement. Conflict cuts **confidence**, not the score — the reading stays honest and the certainty drops. `CONFLICT_HIGH` caps the signal state at `WATCH`; `MEDIUM` caps it below `A_PLUS` |
| Scores presented as probabilities | `MODEL SCORE` until earned. `calibration.kind` is `PROBABILITY` only after ≥30 observations resolve in that score bucket, and observations are opened before the outcome exists and resolved from strictly later prices |
| Pre-break features hidden behind one number | All ten exposed separately |
| "No resistance identified below visible swings" | Levels are built from the engine's own 15-second bars when exchange candles are too coarse to have produced swings yet. It was a bug, not a state |
| Numbers jittered, labels re-rendered every tick | Calculation and display are separate: the engine ticks as fast as data arrives, the panel repaints on a 300 ms throttle. DOM is built once and only values change. `font-variant-numeric: tabular-nums` so digits do not reflow. Colour changes pass a deadband |
| One "latency" number | Split: `WS_LATENCY`, `BOOK_AGE`, `TRADE_AGE`, `PROCESSING_LATENCY`, `UI_LATENCY`. A new `STALE_DATA` status, distinct from `DEGRADED`, disables signals rather than scoring old numbers |

## Added

**Engine** — `normalize.py`, `layers.py`, `calibration.py`, `candles_rest.py`,
`fibonacci.py`.

**Market Workspace** — `/lead-engine/{SYMBOL}`:
`lead_workspace.html`, `lead_workspace.js`, `lead_workspace.css`.
A candlestick chart on 1m–1D, history fetched once and the live candle
updated incrementally rather than the series redrawn; EMA 9/18/50/200
toggleable with the choice saved; an interactive Fibonacci retracement
tool (`lead_fib.js`) with iPhone touch support, correct handling in both
directions, drawings saved per symbol **and** timeframe; Lead Engine
signal markers placed at their real timestamps, never at the current bar.

**Panel** — `lead_panel.js` (build-once renderer with deadband),
`lead_blocks.js` (block definitions and frame application).

**Tests** — `tests/test_lead_engine_math.py` (34).

## Fixed along the way

* `aggregate_candles` dropped every bucket on non-5m input because the
  base interval was hard-coded — `base_seconds_of()` infers it.
* Walls were only registered from order-book *deltas*, so a wall that
  appeared in a snapshot and then vanished was never classified.
* Spoof detection was unreachable: it asked whether best bid ≤ the bid
  wall's price, which is true the instant the wall is pulled.
* `EXHAUSTION` was unreachable — peak velocity was compared against a
  minute average; now 1-second buckets rolled over 5.
* `fading_bounces` was always 0 — a bounce is closed by the *return* to
  the level, so only bars that genuinely left it contribute.
* Direction came out "long" on a short market when both pre-break
  probabilities were 0; it now falls back to pressure.
* Nothing was persisted unless a browser happened to be watching — a
  `storage.Recorder` thread now records independently of any viewer.
* A crossed book with contiguous update ids went undetected.

---

# Round 3 — external programmatic access

```
Bybit → Lead Engine → internal state → REST/SSE API → MCP adapter → AI
```

The MCP server is **not** the engine and holds no market state. Every arrow
is a dependency and none points back: the API and the adapter can be
switched off, misconfigured or broken while the engine keeps ingesting
Bybit and keeps scoring.

## Added

**`t3_engine/lead_engine/api_v1.py`** — 19 routes under
`/api/v1/lead-engine`, every one a `GET`. `/snapshot/{symbol}` returns
everything current for one instrument in a single call; `/multi-tf/{symbol}`
returns 4h/1h/15m/5m/1m context; `/stream` is Server-Sent Events carrying
**compact deltas, not the whole state**; `/schema` lets a client discover
the surface at runtime.

**`t3_engine/lead_engine/snapshot.py`** — the AI-facing shape.
`SCHEMA_VERSION = "1.0.0"`.

**`t3_engine/lead_engine/auth.py`** — `EXTERNAL_AI_ACCESS_ENABLED` (the
namespace 404s when off, independently of `LEAD_ENGINE_ENABLED`),
`LEAD_ENGINE_API_KEY` via `Authorization: Bearer` or `x-api-key` compared
in constant time, and a rate limit of 10/s sustained with a burst of 30
per 10 s per token.

**`lead_engine_mcp/`** — the MCP server: `client.py`, `tools.py` (16
read-only tools including `get_multi_tf_snapshot`), `server.py` (JSON-RPC
over stdio, protocol `2024-11-05`, no SDK dependency).

**Docs** — [`API.md`](API.md), [`MCP.md`](MCP.md).

**Tests** — `tests/test_lead_engine_external.py` (38), including an AST
check that no module in `lead_engine_mcp` imports `t3_engine` at all.

## Read-only, structurally

Not a policy — the import graph. There is no write path in either package
to expose: it cannot place an order, close one, change leverage, touch an
exchange credential, move funds, or reach the analyser's execution engine.
Tests assert that every route on the v1 router is a `GET`, that no handler
name looks like a mutation, and that a trading tool simply does not exist
in the MCP tool list.

## Freshness, on every response

`server_time`, `data_age_ms`, the split per-feed ages, `quality`
(`ok`/`degraded`/`stale`) and `signals_valid`. When the feed is stale the
API says so and sets `signals_valid: false` rather than serving old
numbers as current.

---

# Round 5 — the engine marked against itself

Every round so far added ways for the engine to report a score. A
microstructure engine that only ever reports scores cannot be wrong about
anything, and "LONG_PRESSURE 71" is unfalsifiable in a way that a P&L is
not. This round makes it wrong in public.

## Added

**`t3_engine/lead_engine/virtual_trades.py`** — a paper ledger per symbol.
Every actionable signal opens a virtual position, every position closes
for a stated reason, and the ledger says what it cost. Not a backtest: it
runs on the live stream, against the same book the signal was computed
from.

Six rules, each of which exists to stop it flattering itself:

1. **Entry is never at the signal's own price.** The fill comes from the
   next book that arrives *strictly after* the signal, so the decision and
   the execution never share an instant.
2. **Entry crosses the spread**, then pays slippage on top. A long pays
   the ask, a short hits the bid. Filling at the mid is a half-spread of
   free money per trade — on a 1bp spread and a hundred trades, a
   strategy's entire edge.
3. **No fill is invented inside a gap.** If no fresh book arrives within
   `max_fill_gap_ms` of the signal, the intent is `ABANDONED` — a trade
   that did not happen, recorded as such, rather than a trade at a price
   nobody saw. An exit that had to be taken across an observed gap is
   flagged `gap_uncertain` rather than quietly booked.
4. **Exits are stop, target, or time.** A position that is never closed is
   a position that never loses.
5. **Fees both ways, always**, deducted from the gross and reported
   separately from it.
6. **A stale book is not a price** — one older than the engine's own
   DEGRADED threshold fills nothing, entry or exit.

Two subtleties that are easy to get wrong and are pinned by tests:

* **One clock for ordering, the other for the record.** A signal carries
  this process's clock, a book carries the exchange's. Comparing them
  directly makes the entry rule depend on the offset between them — at
  80ms, every fill is 80ms late; at a negative offset, the ledger would
  fill on a book stamped *before* the signal. Every ordering decision is
  made on the arrival clock; the exchange stamp is kept alongside it
  (`entry_book_ms`, `exit_book_ms`) because that is when the price was
  true.
* **The book that fills a position does not also settle it.** Otherwise a
  spread wider than the stop closes every trade on entry — the long pays
  the ask, the bid is a full spread below it, and the ledger books a stop
  that no price movement caused.

`open_pnl` is marked at what it would cost to **close**, not at the mid.

**Wiring** — `SymbolState.on_book` is fed from `on_orderbook` and nowhere
else; `on_signal` from `_compute_snapshot` and nowhere else. That ordering,
not a comparison inside the ledger, is what makes rule 1 structural: when
the snapshot runs, every book it saw has already been offered to the
ledger and rejected as not-after. A test asserts the call sites.

**`GET /api/lead-engine/virtual-trades/{symbol}`** and
**`GET /api/v1/lead-engine/virtual-trades/{symbol}`** — summary plus
journal. Every frame also carries `virtual_trades` with the last six rows,
so the panel needs no second request.

**`get_virtual_trades`** — the seventeenth MCP tool.

**The panel** — a *Виртуальные сделки* card: open and closed P&L, fees as
a cost, win rate, the counters that make the rules auditable
(`abandoned_on_gap`, `gap_uncertain_exits`, `skipped_stale_book`), and a
journal grid that folds to four columns on a phone. `Panel.html()` was
added for it — the journal is the one block whose *shape* varies, so it
cannot be a fixed set of cells built once; the "write only if it changed"
rule still applies.

**Tests** — `tests/test_lead_engine_virtual_trades.py` (30), one per rule,
each written so that removing the rule makes it fail.

## Paper only, structurally

The module holds no credential, imports no HTTP client, and contains no
order verb; a test asserts each of those on the source. Like everything in
the v1 namespace the endpoint is a `GET` that reports what the paper
positions did — it cannot open, close or size anything on an exchange.

---

# Round 6 — a surface for the money

Both books were running and neither had one. "я не вижу где алгоритм
торгует, нет ни вкладки доходности ничего нет" was a correct reading of
the application: the paper engine's equity, positions and fills existed
only inside the process, and the virtual ledger was a card at the bottom
of a long panel. A paper engine whose P&L you cannot see is
indistinguishable from one that is not trading at all.

## Added

**`GET /api/live/performance`** — both books, side by side, never summed:

* `paper` — the Elliott strategy on CLOSED candles through the same
  `BacktestEngine` a backtest uses: equity per timeframe, open positions
  marked to the live price, take-profit legs, fills, realized and
  unrealized P&L, and the database journal that survives a restart.
* `lead` — the microstructure ledger from Round 5.

They disagree by construction — different signals, different horizons,
different instruments — so a single blended number would hide which of the
two is working. There isn't one, and a test asserts there isn't.

**The Доходность tab** (`static/pnl.js`), polling only while visible.

## Three things it would have been easy to get wrong

**An open position marked at its entry price never loses.** The mark is
the freshest price the session has seen — the forming bar, which knows it
before any closed candle does — not the entry and not the last closed
candle, which on 4h can be hours stale.

**Equity is not summed across timeframes.** Every timeframe runs its own
book off the same nominal capital, so five 10 000 books are not 50 000 of
capital; reporting that invents money that was never allocated. What IS
additive is the money made, and that is what the card shows.

**A take-profit leg carries a `fraction`, not a `quantity`.** Reporting a
`quantity` field would have been a silent `None` on every row.

**An empty journal is explained, not left blank.** In `ai_only` mode a
timeframe cannot open anything until a saved AI count exists for it, and
the strategy trades closed candles on 5m and up — so twenty minutes of
uptime is a handful of bars, not a handful of missed setups. The panel
says which of those it is.

## Fixed: two counters that could not answer their own question

**`resyncs` counted the first sync.** The engine resets every book before
subscribing — correct, and a no-op on an empty book — but the counter
incremented anyway, so a clean seventeen-minute run reported `resyncs=7`
on seven symbols before the first frame had arrived. Read literally that
failed an acceptance criterion the run had passed. A reset of a book that
was never synced is no longer counted.

**The freshness percentiles were computed since process start**, which
cannot answer "median under 250ms AND NOT DRIFTING": such a statistic lags
by construction and never forgets a bad minute. On the live run one
1.2-second stall on the link to Bybit — twenty seconds, self-recovered,
nothing dropped, no resync — pushed the cumulative p95 from 702ms to
832ms and left it there for the rest of the run, which reads as an engine
degrading and was nothing of the sort. The heartbeat now reports a
five-minute window alongside the lifetime figures.

**`data_failure` was a bare number.** One flickering symbol out of seven
looked identical to a feed-wide problem. The line now names the symbol and
the signal machine's own reason — which is how the stall above was
diagnosed in one reading rather than guessed at.

## Also in Round 6 — the engine as a ChatGPT app

`lead_engine_mcp/widgets.py`. A connector returns JSON and the model reads
it out loud; an app returns a component. Three tools now render one:
`get_market_snapshot`, `get_virtual_trades`, `get_health`.

Served as MCP resources (`ui://widget/<name>.html`, mimeType
`text/html+skybridge`), opted into per tool through
`_meta["openai/outputTemplate"]`. `initialize` advertises the `resources`
capability, so a host that knows nothing about widgets never asks and
receives exactly the JSON it received before.

**The split that keeps it cheap.** `structuredContent` reaches the widget
AND the model's context; `_meta` reaches the widget only. So the summary
goes in the first and the forty-row journal in the second — putting rows
in `structuredContent` is the commonest way an app becomes slow and
expensive without ever looking wrong.

**No widget touches the network.** They render from `toolOutput` and
nothing else. A widget that fetched would need the API token inside the
browser, publishing a credential to everyone who can see the
conversation, and could show the person something the model never saw. A
test asserts every network primitive is absent from the widget HTML, and
another re-asserts that this layer, like the rest of the package, imports
no `t3_engine`.

**Protocol negotiation**, found while checking that a host could actually
connect. `initialize` answered with a hard-coded `2024-11-05` whatever the
client asked for. The spec has the client state its version and the server
answer with the one it will speak, so a client on a newer revision was
being told this server only does an older one — and a strict host can
refuse over a difference that does not exist, since every method here
behaves identically on all three revisions. The server now echoes the
client's version when it is one of `2025-06-18`, `2025-03-26` or
`2024-11-05`, and otherwise answers with its newest.

---

# Round 7 — the ledger survives the restart

The free Render plan stops a web service about fifteen minutes after the
last inbound HTTP request. The Bybit socket is outbound and does not
count, so the engine was being stopped several times an hour — once for
two hours and nineteen minutes straight — and an in-memory P&L reported
zero every time it came back. A ledger that resets four times an hour is
indistinguishable from a strategy that never traded, which is exactly the
complaint Round 6 set out to answer.

## Added

**`lead_engine_virtual_trades`** — one row per terminal paper trade,
keyed on `trade_id` (symbol, signal time, sequence), which the ledger
already assigns.

**`VirtualTrade.to_row()` / `.from_row()`**, and on the ledger
`drain_persist()` / `restore()`.

## Queued, not written — and that is the whole design

`_retire` is reached from the INGEST thread. An HTTP call there would
stall the socket reader, which is precisely the failure the two-thread
ingest exists to prevent. So the ledger performs no I/O at all: it queues
terminal trades and the recorder — on its own thread, where a slow write
costs a sweep rather than a dropped frame — drains them onto the existing
batched write buffer. A test asserts the module imports no database, no
HTTP client, and calls nothing that writes.

The seam is the recorder for reading too: the engine still holds no
database handle, which is what kept that boundary checkable in Round 1.

## Four rules that keep a restart honest

**An OPEN position from a dead process is not open.** Nobody was marking
it against the book while the process was down; its stop was never
checked and its deadline never tested. Restoring it would eventually book
an exit at a price nobody observed — RULE 3 again, applied to a gap the
size of a restart. Only CLOSED and ABANDONED come back.

**Restoring twice does not double the P&L.** The id is the identity, and
a row already in the journal is skipped.

**A restored trade is never written back.** It came *from* storage;
re-queueing it would grow the table by its own contents on every restart.

**A resend upserts.** The natural key rejects duplicates with 409, and the
write buffer puts failed rows back and retries — so without
`on_conflict=trade_id` a single already-written row would block every row
behind it, permanently. `supabase_rest.insert` gained the option for
exactly this; tables without a natural key keep plain-insert semantics.

Reads are attempted once per symbol, whatever the result: retrying a
failed read every sweep would turn one Supabase outage into a request a
second per symbol, forever. A ledger that cannot read its history starts
empty — losing the history makes a worse report, failing to start makes a
worse engine.

## Reported, not implied

`summary()` carries `restored_from_storage`, and both panels show it.
"42 trades" means something different when 40 of them predate the current
process, and the reader should not have to guess which.

The paper (Elliott) engine's positions are still memory-only; the panel
says so rather than letting its zero read like the ledger's total.

Verified against the live table: both row shapes accepted, nulls
included, the conflict clause merges rather than duplicating, and
`restore()` reads the exact shape PostgREST returns.

---

# Round 8 — why the algorithm was not opening trades

Reported with a screenshot: the order book visibly trading, and the panel
reporting "no resistance identified above the current price in the visible
swings", zero levels, zero signals, zero virtual trades. Two separate
defects, one of them arithmetic.

## The engine started cold, and three minutes is not enough

The level tracker built its history from the trade stream at fifteen
seconds a bar and had no history at all before that. Three minutes in it
holds twelve closed bars — plenty by count, which is why this never looked
like a starvation problem. But a fractal swing needs a bar higher than the
two that FOLLOW it, and in a trending stretch no bar ever is. Reproduced
exactly: twelve bars, zero swings, and a test now states it as arithmetic
rather than as a complaint.

Zero swings → zero levels → no PRE_BREAK → no direction → no virtual
trade, while the tape runs. And on the free Render plan the process is
stopped every fifteen minutes, so it never accumulated the ten-odd minutes
the fine series needs to turn at least once. The engine spent most of its
life unable to name a level.

**`LeadEngine._backfill_structure`** seeds each symbol's minute series
from Bybit REST at start — four hours, one request per symbol, on its own
thread, and it never raises: an engine that cannot reach REST is an engine
with less history, not a dead one.

**The fallback rule was also wrong.** It read "use the minute series when
the fine one is EMPTY", and empty is not the failing case — twelve
useless bars are not empty. It now reads "…until the fine one has
`MIN_CLOSED_BARS_FOR_LEVELS` closed bars of its own", which is ten
minutes of 15-second history: long enough that the series has certainly
turned. Below that the seeded minutes are simply the better answer,
because they reach back before this process existed.

Measured on the same trending three minutes: 0 swings and 0 levels cold,
60 swings and 7 levels seeded.

## A bar with no value was drawing itself full

`.le-feature .bar > i` is a block element with no width in the
stylesheet, so it filled its parent. Before any value arrived — and
whenever the pre-break engine finds no level and returns **no features at
all** — every one of the ten bars rendered at 100%. An engine that had
found nothing was drawing itself as an engine that had found everything,
which is what the screenshot shows.

Two fixes, because either alone leaves a hole: `width: 0` in both
stylesheets, and the feature loop now walks a declared list rather than
the keys the engine happened to return, clearing the absent ones to "—"
and zero. A loop over `Object.keys(features)` on an empty map writes
nothing, which also left the PREVIOUS frame's numbers on screen as if
they were current.

## And the diagnosis was in the API but not on the screen

`LevelTracker.diagnose` has distinguished "not enough history" from "no
swing confirmed" from "every level is on the wrong side" since the
diagnostics went in. It reached `/state` and was never rendered, so the
panel kept saying the one sentence that cannot be acted on. The pre-break
cards now show the reason and the counts underneath it — but only when
there is no level, since with one the note above already says everything.


---

# Round 9 — the audit, and a tuning that was not done

Asked to re-check everything, run it against history, and tune it to
perfection.

## What was checked

* **957 tests pass.**
* **Replay of 10,288 recorded Bybit frames** through a fresh engine: zero
  errors, 15,000 frames/s, the book synced across 2,400 deltas with zero
  gaps, zero resyncs and zero crossed books, and every ledger invariant
  holding (no fill at or before its own signal, no exit before its entry,
  `net == gross - fees`).

  The first replay pass reported DATA_FAILURE on all twenty snapshots -
  correctly. The capture is two days old and the health module measures
  staleness against the wall clock, so the signal machine was muted and
  the pass proved the ingest path and nothing about the decisions. Re-run
  with the capture's own clock: WATCH 35, IDLE 16, max break score 44.2 -
  consistent with the live recording, and the confidence gate and the
  CONFLICT_HIGH block both observed firing.

## A bug this found, in code from the previous round

`SmcEngine.update` appended any candle that was not the newest, leaving
the series non-monotonic - and `find_swings` compares each bar with its
neighbours BY INDEX and never reads a timestamp. Out-of-order bars
therefore produce a different swing set, different levels and a different
break score from the same data.

The REST backfill made this reachable: it seeds 240 historical minutes on
its own thread while the kline socket is already running, so on a warm
start the live bars arrive first and the history lands behind them - and
deterministically so on the `subscribe()` path, where the stream never
stopped. Measured on 240 bars plus twelve warm minutes: nearest support
**98.27 in order, 98.13 out of order**.

`update` now places a candle in time order, replacing rather than
duplicating a minute that is already there. The two paths the socket
actually takes - same bar as the newest, or strictly newer - stay O(1).

## The tuning that was not done

See [`CALIBRATION_FINDINGS.md`](CALIBRATION_FINDINGS.md). Short version:
28.7 hours of recorded output, properly de-duplicated into 45 independent
episodes across ten hours, shows an apparent edge that **inverts** when
the horizon moves from 30 to 60 minutes, and that nets -1.8 bps against
13 bps of round-trip cost. The binding constraint is costs, not the
threshold: a signal whose median favourable excursion is 11.7 bps cannot
pay 13 bps to trade.

The thresholds were left alone. Lowering them on this evidence would
manufacture trades with negative expected value, which is worse than not
trading because it would look like progress.

## A flaky test, made deterministic

`test_the_rate_limit_bites_but_leaves_room_for_a_reading_a_second` fired
sixteen real HTTP requests and expected a 429 among them. That asserts
something about the machine, not about the limiter: the refusal only
appears if eleven round trips complete inside the limiter's one-second
window. It passed alone and failed inside the full suite, having found
nothing wrong.

`RateLimiter.check` already takes the clock as an argument, so the
counting is now tested against a clock the test controls - the per-second
rule, the burst cap, the cap lifting, and one token's noise not spending
another's budget. A single HTTP test remains for the part only HTTP can
prove: that the limiter's refusal reaches the caller as a 429. All four
were checked against a deliberately broken limiter first; three fail when
the per-second rule is disabled, so they bite.
