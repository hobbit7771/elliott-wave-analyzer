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
