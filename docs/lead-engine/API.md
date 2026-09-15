# Lead Engine external API — `/api/v1/lead-engine`

The read-only, token-gated, versioned HTTP surface an external agent uses
to read this engine. It is an **adapter, not the engine**. The dependency
runs one way:

```
Bybit  ->  Lead Engine  ->  internal state  ->  REST / SSE  ->  MCP  ->  AI
```

Nothing downstream of the engine is a dependency of it. Switch this API
off, misconfigure it or break it and the Lead Engine keeps ingesting Bybit
and keeps scoring; only the external view goes away.

This is separate from `/api/lead-engine/*`, which serves the dashboard's
own tab same-origin and unauthenticated. The split is deliberate — mixing
them would mean loosening one set of rules to fit the other.

---

## 1. Read-only, structurally

Every route is a `GET`. There is no write path in this package to expose.
It cannot place an order, close one, change leverage, touch an exchange
credential, move funds, or reach the analyser's execution engine.

That is not a policy, it is the import graph: `tests/test_lead_engine_isolation.py`
asserts the modules that would be needed are never imported, and
`tests/test_lead_engine_external.py` asserts every route on the router is
a `GET` and that no handler name looks like a mutation.

## 2. Enabling it

Two independent switches, both off by default:

| Variable | Meaning |
| --- | --- |
| `LEAD_ENGINE_ENABLED` | the engine itself ingests Bybit |
| `EXTERNAL_AI_ACCESS_ENABLED` | this external namespace answers at all |
| `LEAD_ENGINE_API_KEY` | the read-only token every request must carry |

`T3_`-prefixed forms (`T3_EXTERNAL_AI_ACCESS_ENABLED`, `T3_LEAD_ENGINE_API_KEY`)
are accepted too, matching the rest of the project's configuration.

The key lives **only** in the deployment's environment variables. It is
never in the repository, never in a frontend bundle, and never in a
response body — the `/schema` route names the env var, not its value.

Responses when a switch is off:

| Condition | Status | Body |
| --- | --- | --- |
| `EXTERNAL_AI_ACCESS_ENABLED` unset | `404` | the namespace does not exist |
| flag on, `LEAD_ENGINE_API_KEY` unset | `503` | the deployment has no token configured |
| token missing or wrong | `401` | |
| over the rate limit | `429` | |

## 3. Authentication

Either header, whichever the client finds easier:

```
Authorization: Bearer <token>
x-api-key: <token>
```

The comparison is constant-time. There is no cookie, no session and no
same-origin path into this namespace.

## 4. Rate limit

10 requests/second sustained, burst 30 per 10 seconds, per token. Over the
limit returns `429` with a `retry_after` field. The live numbers are
readable from `/schema` (`rate_limit.per_second`, `.burst`,
`.burst_window_seconds`) rather than hard-coded by the client.

## 5. Freshness — on every response, without exception

A client must never have to guess whether what it just received is
current. Every response carries:

| Field | Meaning |
| --- | --- |
| `server_time` | epoch ms when the response was built |
| `engine_running` | is the ingest loop alive |
| `read_only` | always `true` |
| `api_version` | `"v1"` |

Per-symbol responses add:

| Field | Meaning |
| --- | --- |
| `data_age_ms` | age of the oldest input the scores rest on |
| `book_age_ms`, `trade_age_ms`, `oi_age_ms`, `ws_latency_ms` | split per feed |
| `quality` | `ok` / `degraded` / `stale` |
| `signals_valid` | `false` whenever the feed is stale |
| `engine_status` | `OK` / `DEGRADED` / `STALE_DATA` / `DATA_FAILURE` |

**Do not act on a snapshot whose `quality` is not `ok` or whose
`signals_valid` is `false`.** The engine disables its own signals on stale
data rather than scoring old numbers, and says so here instead of hiding it.

## 6. Endpoints

All paths are prefixed `/api/v1/lead-engine`. `{symbol}` is a Bybit linear
symbol, e.g. `INJUSDT` (case-insensitive).

### Discovery and status

| Path | Returns |
| --- | --- |
| `GET /schema` | what this API offers: endpoint list, schema version, timeframes, auth header forms, live rate limit, and the interpretation notes |
| `GET /status` | engine running state, stream statistics, every symbol's headline |
| `GET /symbols` | subscribed instruments, the BTC reference symbol, servable timeframes |
| `GET /health` | per-symbol feed freshness plus WebSocket statistics |

### The one call that matters

| Path | Returns |
| --- | --- |
| `GET /snapshot/{symbol}` | **everything current for one symbol in one request** |

This is the endpoint an AI client should normally use. One round trip,
one coherent moment — not eight calls that each see a slightly different
tick. Top-level shape:

```jsonc
{
  "schema_version": "1.0.0",
  "symbol": "INJUSDT",
  "tracked": true,
  "price": 5.7238,
  "mark_price": null,

  "long_pressure": 17.08,          // 0..100 MODEL SCORE, not a probability
  "short_pressure": 0.0,
  "pressure_confidence": 0.449,    // 0..1
  "conflict": "CONFLICT_LOW",      // CONFLICT_HIGH | CONFLICT_MEDIUM | CONFLICT_LOW
  "conflict_note": "",

  "break_score_long": 0.0,
  "break_score_short": 0.0,
  "calibration": {                 // see section 7
    "long":  {"kind": "MODEL_SCORE", "model_score": 0.0, "probability": null, "note": "..."},
    "short": {"kind": "MODEL_SCORE", "model_score": 0.0, "probability": null, "note": "..."}
  },

  // the five independent layers, each scored on its own evidence
  "flow":        {"score": 0.2339, "long": 23.39, "short": 0.0, "confidence": 0.88,
                  "label": "LEAN_BULLISH", "detail": {...}, "missing": ["large_trade_imbalance"]},
  "book":        {"score": 0.1635, "confidence": 0.52, "label": "BOOK_BULLISH", ...},
  "structure":   {"score": 0.0,    "confidence": 0.0,  "label": "NO_STRUCTURE", ...},
  "derivatives": {"score": 0.0,    "confidence": 0.0,  "label": "NEUTRAL", ...},
  "btc_lead":    {"score": 0.0,    "confidence": 0.55, "label": "BTC_LEAD_NEUTRAL", ...},

  "microstructure": {
    "best_bid": 5.70, "best_ask": 5.702, "spread": 0.002, "spread_bps": 3.51,
    "microprice": 5.70125,
    "obi_1": 0.25, "obi_5": 0.25, "obi_10": 0.25, "obi_25": 0.25, "obi_50": 0.25,
    "weighted_obi": 0.25,
    "book_alignment": 0.25, "book_alignment_label": "BOOK_BULLISH",
    "bid_pulling": 0.0, "ask_pulling": 0.0,
    "bid_replenishment": 0.0, "ask_replenishment": 0.0,
    "walls": {"walls": [], "by_classification": {}, "wall_bias": 0.0,
              "spoofs_60s": 0, "cancellations_60s": 0},
    "cvd": 160.0, "cvd_5s": 100.0, "cvd_60s": 164.0, "cvd_divergence": null,
    "normalized_delta_5s": 0.3846, "normalized_delta_60s": 0.3333,
    "trade_velocity": 6.0, "velocity_zscore": 1.43, "velocity_state": "normal"
  },

  "open_interest": {"value": null, "delta": null, "delta_pct": null,
                    "trend": "unknown", "interpretation": "unknown", "age_ms": null},
  "liquidations":  {"state": "NEUTRAL", "velocity": 0.0, "peak_velocity_60s": 0.0,
                    "windows": {"1s": {...}, "5s": {...}, "15s": {...}, "60s": {...}}},
  "smc":           {...},          // HH/HL/LH/LL, BOS, CHoCH, sweep, FVG, OB, premium/discount
  "elliott":       {"current_wave_candidate": null, "phase": "unknown",
                    "direction": "flat", "legs_counted": 0, "source": "internal"},
  "support_levels":    [...],      // every level the pre-break engine is watching
  "resistance_levels": [...],
  "active_signal": {"state": "IDLE", "direction": "", "confidence": 17.08,
                    "reason": "nothing worth naming", "level": null,
                    "break_probability": 0.0, "changed_at": 1789491439.88},

  "health": {...},
  "data_age_ms": 39.0, "book_age_ms": 39.0, "trade_age_ms": 39.0,
  "ws_latency_ms": 0.0, "engine_status": "OK",
  "quality": "ok", "signals_valid": true,
  "server_time": 1789491439926, "engine_running": true,
  "read_only": true, "api_version": "v1"
}
```

`support_levels` and `resistance_levels` are always populated when the
engine has price history — including levels built from its own 15-second
bars when the exchange's candles are too coarse to produce swings yet.
"No resistance identified below visible swings" was a bug, not a state.

### Multi-timeframe context

| Path | Returns |
| --- | --- |
| `GET /multi-tf/{symbol}` | 4h / 1h / 15m / 5m / 1m in one call |

Per timeframe: OHLC summary, EMA 9/18/50/200, the recent swing highs and
lows, trend label, distance to each EMA, and the position of price inside
the timeframe's range. This is the context call — one request instead of
five `/candles` + five `/indicators`.

### Slices, for a client that wants one thing

| Path | Returns |
| --- | --- |
| `GET /market/{symbol}` | price, spread, microprice, headline scores |
| `GET /orderbook/{symbol}` | L2 state: OBI 1/5/10/25/50, weighted OBI, alignment, walls with their classification, absorption |
| `GET /flow/{symbol}` | normalised delta over eight windows, CVD, divergence, velocity |
| `GET /derivatives/{symbol}` | open interest with its four-way price/OI reading, liquidation windows, flush state |
| `GET /structure/{symbol}` | SMC state and the pre-break engine's levels |
| `GET /pressure/{symbol}` | the five layers, weights, confidence, conflict detail |
| `GET /signals/{symbol}` | current signal state, direction, level under stress, break score |
| `GET /state/{symbol}` | the raw internal frame (everything, unshaped) |
| `GET /history/{symbol}` | recent feature history — pressure, break scores, state over time |

### Chart data

| Path | Query | Returns |
| --- | --- | --- |
| `GET /candles/{symbol}` | `timeframe` (`1m 3m 5m 15m 30m 1h 4h 1d`), `limit` | OHLCV oldest first; the newest row carries `closed: false` |
| `GET /indicators/{symbol}` | `timeframe`, `limit` | EMA 9/18/50/200 over the same closes the chart draws |
| `GET /fibonacci/{symbol}` | `timeframe` | saved retracements with every level priced |

The unclosed newest candle is marked rather than dropped, so a client can
choose: use it for the live price, exclude it from anything that must be
closed-candles-only.

### Stream

| Path | Query | Returns |
| --- | --- | --- |
| `GET /stream` | `symbol`, `interval_ms` (200–5000, default 500) | Server-Sent Events |

**Compact deltas, never the whole state.** The full snapshot is tens of
kilobytes and almost none of it moves between ticks; each event carries
only the fields whose value actually changed, so a quiet market produces
almost no traffic.

```
event: hello
data: {"symbol":"INJUSDT","interval_ms":500}

event: update
data: {"price":5.7238,"long_pressure":17.08,"short_pressure":0.0,"confidence":0.449,
       "conflict":"CONFLICT_LOW","break_long":0.0,"break_short":0.0,"signal":"IDLE",
       "signal_direction":"","obi_5":0.25,"book_alignment":0.25,"cvd":160.0,
       "status":"OK","signals_valid":true,"book_age_ms":39.0,"t":1789491439926}

: keepalive

event: update
data: {"price":5.7241,"cvd":164.0,"t":1789491440426}
```

The tracked fields are `price`, `long_pressure`, `short_pressure`,
`confidence`, `conflict`, `break_long`, `break_short`, `signal`,
`signal_direction`, `obi_5`, `book_alignment`, `cvd`, `status`,
`signals_valid`, `book_age_ms`, plus `t` (epoch ms). A `: keepalive`
comment goes out when nothing changed, which holds the connection and any
proxy in front of it open without costing the client a parse.

SSE rather than a WebSocket because this is one-directional by nature —
the client subscribes and reads. SSE reconnects itself, passes through
every proxy that passes HTTP, and needs no framing library.

For the full state, call `/snapshot/{symbol}` once and apply deltas to it.

## 7. MODEL SCORE, not probability

`long_pressure`, `short_pressure`, `break_score_long` and
`break_score_short` are **model scores on 0–100**. They are a weighted
combination of normalised features. They are *not* the probability of
anything, and the API refuses to call them one.

`calibration.long.kind` says which you are holding:

| `kind` | Meaning |
| --- | --- |
| `MODEL_SCORE` | not calibrated yet — `probability` is `null`, the `note` says so |
| `PROBABILITY` | ≥30 resolved observations in this score bucket; `probability` is the empirical hit rate |

The calibrator opens an observation *before* the outcome exists and
resolves it only from strictly later prices, so a rate it offers was never
fitted on the future it is predicting. Until a bucket reaches 30 samples,
there is no honest number to give and the API gives none.

## 8. Versioning

The path carries the major version (`/api/v1/`). Within v1, fields are
added but never removed or repurposed; `schema_version` in the body
(currently `1.0.0`) moves when fields are added. A client should read the
fields it knows and ignore the rest.

## 9. OpenAPI

The whole surface is in the app's generated OpenAPI document:

- `GET /openapi.json` — machine-readable
- `GET /docs` — Swagger UI
- `GET /redoc` — ReDoc

## 10. Examples

```bash
export LEAD_ENGINE_API_KEY=...      # never commit this

curl -H "x-api-key: $LEAD_ENGINE_API_KEY" \
  https://t3-elliott-wave-engine.onrender.com/api/v1/lead-engine/snapshot/INJUSDT

curl -H "Authorization: Bearer $LEAD_ENGINE_API_KEY" \
  "https://t3-elliott-wave-engine.onrender.com/api/v1/lead-engine/multi-tf/INJUSDT"

curl -N -H "x-api-key: $LEAD_ENGINE_API_KEY" \
  "https://t3-elliott-wave-engine.onrender.com/api/v1/lead-engine/stream?symbol=INJUSDT&interval_ms=500"
```

A client that reads one symbol continuously should open `/stream` and
seed it with a single `/snapshot` call, not poll `/snapshot` in a loop.

## 11. See also

- [`MCP.md`](MCP.md) — the MCP adapter that sits on top of this API
- [`PRESSURE_SCORE.md`](PRESSURE_SCORE.md) — how the five layers are combined
- [`SIGNALS.md`](SIGNALS.md) — the signal state machine
- [`ORDERBOOK.md`](ORDERBOOK.md) — OBI, walls, absorption
- [`PREBREAK.md`](PREBREAK.md) — level detection and break scoring
