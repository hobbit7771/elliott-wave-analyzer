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
