# Market Lead Engine — architecture

A realtime market-microstructure engine that lives inside this repository
and is not part of the Elliott wave analyser it sits beside. It shares the
web process, the page layout, the Supabase account, the deployment and the
logging setup. It shares no code, no state, no tables and no endpoints.

## Where the line is

```
t3_engine/
├── lead_engine/              ← the new engine. Self-contained.
│   ├── config.py             flag, symbols, layer weights, thresholds
│   ├── bus.py                lead_engine.* event bus
│   ├── rolling.py            time-windowed accumulators, z-scores
│   ├── normalize.py          rolling z-score / percentile / ratio → [-1,+1]
│   ├── bybit_ws.py           its own Bybit socket, its own thread
│   ├── orderbook_engine.py   L2 book from snapshot+delta, OBI, walls
│   ├── microprice.py         microprice history, deltas, bias
│   ├── trade_flow.py         taker flow, velocity, large prints
│   ├── cvd.py                cumulative delta and price/flow divergence
│   ├── candles.py            candles built from trades
│   ├── candles_rest.py       Bybit REST OHLCV + EMA, for the chart
│   ├── liquidation_engine.py forced closes, flush states
│   ├── oi_engine.py          open interest, polled on its own thread
│   ├── btc_leadlag.py        correlation, lag estimate, BTC impulse
│   ├── smc_engine.py         its own structure read
│   ├── elliott_state.py      its own wave context (or a read-only adapter)
│   ├── prebreak_engine.py    levels and break scoring
│   ├── layers.py             the five independent layers + conflict detector
│   ├── pressure_engine.py    combines the layers into LONG/SHORT pressure
│   ├── calibration.py        model score → empirical probability, or nothing
│   ├── signal_machine.py     the ten states
│   ├── health.py             split latencies; is the data good enough
│   ├── fibonacci.py          retracement levels + per-symbol/TF drawings
│   ├── state.py              one symbol's world
│   ├── engine.py             LeadEngine — the facade
│   ├── storage.py            lead_engine_* tables
│   ├── replay.py             replay + backtest metrics
│   ├── snapshot.py           the AI-facing shape: snapshot, multi-TF context
│   ├── auth.py               external flag, token, rate limit
│   ├── api.py                /api/lead-engine/*        (the dashboard tab)
│   └── api_v1.py             /api/v1/lead-engine/*     (external, token-gated)
└── (everything else)         ← the analyser. Untouched.

lead_engine_mcp/               ← the MCP adapter. Imports NO t3_engine at all.
├── client.py                  HTTP to /api/v1/lead-engine
├── tools.py                   16 read-only tools
└── server.py                  JSON-RPC over stdio
```

### The one-directional chain

```
Bybit → Lead Engine → internal state → REST/SSE API → MCP adapter → AI
```

Each arrow is a dependency, and none of them points back. The MCP adapter
holds no market state; the API holds no market state; both can be switched
off, misconfigured or broken while the engine keeps ingesting and scoring.
`lead_engine_mcp` is checked by AST never to import `t3_engine`, so it can
be copied to a client machine and pointed at the deployment — which is the
normal way to run it. See [`API.md`](API.md) and [`MCP.md`](MCP.md).

The dependency rule is one-directional and mechanically enforced by
`tests/test_lead_engine_isolation.py`:

* The Lead Engine imports **nothing** from the analyser except
  `t3_engine.database.supabase_rest` — shared database transport, in the
  same sense as the shared web process. It writes only to its own tables
  through it.
* The analyser reaches the Lead Engine only through `LeadEngine`
  (`engine.py`): `subscribe`, `get_state`, `get_signal`, `get_pressure`,
  `status`. `server.py` may import the router, the config flag and the
  facade, and nothing else — also asserted by a test.
* No Binance. No AI provider. Both asserted per-file, against the code
  with comments and docstrings stripped, so a comment explaining why
  Binance is absent does not count as a dependency.

If a future requirement seems to need more than the facade, the answer is
another facade method or an adapter inside `lead_engine/` — never an
import across the line.

## What runs where

| Thread | Owned by | Does |
|---|---|---|
| web worker | the existing app | serves HTTP, including `/api/lead-engine/*` |
| `lead-engine-ws` | `bybit_ws.BybitLeadStream` | its own asyncio loop, the socket, reconnects |
| `lead-engine-oi` | `oi_engine.OpenInterestPoller` | REST polling for open interest |
| `lead-engine-recorder` | `storage.Recorder` | files features, signal transitions and liquidations on its own clock |

The socket has its **own event loop on its own thread**. The web process's
loop is never used, so a stall in the stream cannot stall a request, and
an exception in the stream cannot unwind into a handler.

## Failure isolation

* `LeadEngine.start()` catches everything and returns a boolean. The
  dashboard's startup hook wraps it again. A Lead Engine that cannot start
  leaves a dashboard that runs without it — exercised by
  `test_a_lead_engine_that_cannot_start_leaves_the_app_running`.
* A subscriber that throws on the bus is logged and skipped; the others
  still receive the event.
* A handler that throws while processing a frame is logged; the socket
  stays connected.
* A storage failure puts rows back in a bounded buffer and never reaches
  the stream thread.
* Conversely, nothing in this engine can affect the analyser: it holds no
  reference to any of its objects and writes to none of its tables.

## The feature flag

`LEAD_ENGINE_ENABLED` (or `T3_LEAD_ENGINE_ENABLED`). Read at call time,
never cached at import.

When false: no socket, no thread, no polling, no timers, and the UI tab
makes no repeated requests. Every route still answers **200 with
`{"enabled": false}`** rather than 404, so the tab can tell "switched off"
from "not deployed".

## Data flow

```
Bybit public linear WS ─┐
                        ├─► BybitLeadStream ─► LeadEngine.handle_message
Bybit REST (open int.) ─┘                            │
                                                     ▼
                                              SymbolState (per symbol)
                    ┌──────────┬──────────┬──────────┼──────────┬──────────┐
                    ▼          ▼          ▼          ▼          ▼          ▼
                 OrderBook  TradeFlow    Cvd    Liquidation  SMC/Elliott  OI
                    └──────────┴──────────┴──────────┴──────────┴──────────┘
                                                     ▼
                                      components ─► PressureEngine
                                                     ├─► PreBreakEngine
                                                     └─► SignalMachine ─► bus, API, UI
```

`replay.py` builds a **second, separate** `LeadEngine` and feeds it
recorded frames through the same `handle_message`. The live engine is
never touched by a replay, and a replay is never influenced by live state.
