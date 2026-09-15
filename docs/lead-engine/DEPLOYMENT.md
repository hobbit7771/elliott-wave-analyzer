# Deployment

## The flag

```
LEAD_ENGINE_ENABLED=true        # or T3_LEAD_ENGINE_ENABLED=true
```

Nothing else is required. With it unset or false the engine is inert: no
socket, no thread, no polling, no timers, and the tab says so. Read at
call time, never cached at import, so a restart is all a change needs.

## Everything else, with defaults

| Variable | Default | What it does |
|---|---|---|
| `T3_LEAD_ENGINE_SYMBOLS` | `BTCUSDT,INJUSDT,DOGEUSDT,AAVEUSDT,ATOMUSDT,FILUSDT,NEARUSDT` | comma separated; BTCUSDT is added if missing |
| `T3_LEAD_ENGINE_WS_URL` | `wss://stream.bybit.com/v5/public/linear` | testing only |
| `T3_LEAD_ENGINE_REST_BASE` | `https://api.bybit.com` | testing only |
| `T3_LEAD_ENGINE_ORDERBOOK_DEPTH` | `50` | levels kept |
| `T3_LEAD_ENGINE_OI_POLL_SECONDS` | `60` | open interest cadence |
| `T3_LEAD_ENGINE_RECOMPUTE_INTERVAL_SECONDS` | `0.25` | how often a frame is rebuilt |
| `T3_LEAD_ENGINE_WEIGHT_<COMPONENT>` | see PRESSURE_SCORE.md | one weight at a time |
| `T3_LEAD_ENGINE_VELOCITY_ZSCORE_ELEVATED` / `_EXTREME` | `2.0` / `3.0` | velocity thresholds |
| `T3_LEAD_ENGINE_PREBREAK_PROBABILITY` | `60` | `PRE_BREAK_*` |
| `T3_LEAD_ENGINE_HIGH_PROBABILITY` | `72` | `HIGH_PROBABILITY` |
| `T3_LEAD_ENGINE_A_PLUS_PROBABILITY` | `85` | `A_PLUS` |
| `T3_LEAD_ENGINE_MAX_BOOK_AGE_SECONDS` | `5` | past this, `DEGRADED` |
| `T3_LEAD_ENGINE_LIQUIDATION_VELOCITY` | `25000` | quote units/sec for a flush |
| `T3_LEAD_ENGINE_WALL_MULTIPLE` | `5` | × median level size |
| `T3_LEAD_ENGINE_LARGE_TRADE_MULTIPLE` | `8` | × median trade size |

## Database

The engine's tables are its own and are created by running the SQL from
`GET /api/lead-engine/schema.sql` (also in `storage.SCHEMA_SQL`) once
against the project's Supabase database:

```
lead_engine_sessions
lead_engine_features
lead_engine_signals
lead_engine_liquidations
lead_engine_backtests
```

No existing table is altered. RLS is enabled on all five, matching the
rest of the project: the service key bypasses it, the publishable key
reaches nothing.

**Without them the engine still runs.** Storage degrades to a bounded
in-memory buffer and `/status` reports `storage.configured: false`.

Writing happens on the engine's **own clock**, not when a browser asks:
`storage.Recorder` walks every symbol every 15 seconds and files a feature
row per symbol, every signal **transition** as it happens, and each
liquidation once. Before that existed nothing was persisted unless someone
had the tab open — a signal that fired at 03:00 left no trace and the
liquidation table stayed permanently empty. `/status` carries
`recorder.sweeps` and `storage.written` so it can be checked.

## Dependencies

`t3_engine/lead_engine/requirements.txt`. Every package there is already a
project dependency — `httpx`, `websockets`, `fastapi` — so enabling this
adds nothing to the image. No numpy, no pandas, no AI SDK, no Binance
client. The maths is stdlib.

## Resource cost

Bounded by construction: 50 book levels, ≤4000 trades, ≤500 liquidations,
≤600 feature snapshots and ≤400 candles per symbol, all trimmed on write.
Two threads for the whole engine, not two per symbol.

## Checking it after a deploy

```bash
curl -s $HOST/api/lead-engine/status | jq '{enabled, running, stream, oi_polling}'
curl -s $HOST/api/lead-engine/state/INJUSDT | jq '.health'
```

* `enabled: false` → the flag is not set on this service.
* `enabled: true, running: false` → it tried and failed; `start_error`
  says why, and the dashboard is unaffected.
* `stream.connected: false` with a `last_error` → the socket is
  reconnecting; the backoff runs from 1s to 30s.
* `health.status: DEGRADED` → `reasons` names the stale stream. Signals
  are muted until it clears.

## Rollback

Set `LEAD_ENGINE_ENABLED=false` and restart. Nothing else in the
application depends on it, so this is a complete rollback — no schema
change to undo, no code path in the analyser to restore.
