# Replay and backtest

`replay.py` builds a **new** `LeadEngine` and feeds it recorded Bybit
frames through `handle_message` — the identical entry point a live socket
uses. The live engine is never touched, so a replay cannot contaminate
live state and live state cannot contaminate a backtest. There is a test
asserting the two engines are distinct objects.

## Capture format

A JSON array of raw Bybit v5 public WebSocket frames, verbatim. Fetch
`GET /api/lead-engine/replay/schema` for the list of topics.

Frames are sorted by exchange timestamp before replay, so a capture that
interleaves topics out of order is still replayed in order. **The capture
must include the order book snapshot**; without one every delta is refused
and the book never syncs, which the report shows as `DATA_FAILURE`
throughout.

## No lookahead, in three places

1. Features are computed by the same code as live, from the same stream,
   in timestamp order. Every window in `rolling.py` filters on
   `cutoff ≤ stamp ≤ reference`, and the structure modules use closed
   candles only.
2. The engine's clock is the **recorded** timestamp
   (`snapshot(now=event_time)`), so a slow replay cannot age a window
   differently from a fast one.
3. Outcomes are evaluated only from prices **strictly after** the signal:
   `_future(t)` returns `t < stamp ≤ t + horizon`.

Tested directly: rewriting the prices *before* a signal leaves its verdict
and its MFE unchanged; rewriting the prices *after* it flips the verdict.

## Metrics

| Metric | Definition |
|---|---|
| **precision** | of the calls that resolved, the share where the level actually broke by `break_pct` within the horizon |
| **recall** | hits ÷ actual breaks in the capture |
| **false positives** | resolved calls where it did not break |
| **lead time** | median milliseconds from the call to the break |
| **MFE** | mean best excursion in the call's favour, as a fraction of price |
| **MAE** | mean worst excursion against it |
| **expected value** | mean of (MFE if it broke, else −MAE), per call |

Only `PRE_BREAK_*`, `HIGH_PROBABILITY` and `A_PLUS` count as calls.
`WATCH` and `PRE_SIGNAL` claim that something is building, not that a
level is about to go, and scoring them would measure the wrong thing.

**`actual_breaks` is crude and is stated as such**: non-overlapping moves
of `break_pct` from a running anchor. It is there so recall means
something — "we called nine and eight worked" says nothing about the ones
missed — and it is not a precise count of level failures.

Expected value is not a strategy result: no sizing, no fees, no funding,
no slippage.

## Running one

```bash
curl -X POST /api/lead-engine/replay/run \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"INJUSDT","messages":[...],"break_pct":0.004,
       "horizon_minutes":10,"store":true}'
```

`store: true` files the report in `lead_engine_backtests`.
