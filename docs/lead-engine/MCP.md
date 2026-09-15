# Lead Engine MCP server — `lead_engine_mcp`

A read-only Model Context Protocol adapter that lets Claude, ChatGPT or any
MCP client read the live Lead Engine as tools.

## 1. It is an adapter, not the engine

```
Bybit  ->  Lead Engine  ->  internal state  ->  REST / SSE API  ->  MCP adapter  ->  AI
```

The MCP server holds **no market state of its own**. It does not connect
to Bybit, does not keep an order book, does not compute a score. Every
tool call is one HTTP GET against `/api/v1/lead-engine/*` (see
[`API.md`](API.md)) and the response is passed through.

This matters for a reason that is easy to get wrong: if the MCP server
were the engine, then an AI client's latency, its rate limit, its
reconnects and its outages would all become the trading engine's problem.
They are not. The engine ingests and scores whether or not anything is
listening.

The isolation is enforced, not just intended:
`tests/test_lead_engine_external.py` walks the AST of every module in
`lead_engine_mcp/` and asserts that **none of them imports `t3_engine` at
all**. The package can be copied to another machine and pointed at the
deployment over the network — that is the normal way to run it.

## 2. Read-only, and it cannot become otherwise

Every tool is a read. There is no tool to open a position, close one,
change leverage, move funds, or touch an exchange key — and none can be
added, because the API underneath has no write path to call. Every route
in `/api/v1/lead-engine` is a `GET`.

## 3. Install and run

One dependency, `httpx`, which the main project already uses. Python 3.9+.

```bash
pip install -r lead_engine_mcp/requirements.txt
```

```bash
export LEAD_ENGINE_API_URL=https://t3-elliott-wave-engine.onrender.com
export LEAD_ENGINE_API_KEY=<the deployment's read-only token>
python -m lead_engine_mcp.server
```

It reads one JSON-RPC message per line from stdin and writes one per line
to stdout. Nothing but protocol messages ever goes to stdout — logging
goes to stderr, because a stray print corrupts the stream and the failure
then looks like a protocol bug rather than a logging one.

### Claude Desktop / Claude Code

```json
{
  "mcpServers": {
    "lead-engine": {
      "command": "python",
      "args": ["-m", "lead_engine_mcp.server"],
      "env": {
        "LEAD_ENGINE_API_URL": "https://t3-elliott-wave-engine.onrender.com",
        "LEAD_ENGINE_API_KEY": "<read-only token>"
      }
    }
  }
}
```

Run it from a checkout of this repository, or copy the `lead_engine_mcp/`
directory anywhere on the client machine — it needs nothing else from the
project beyond `httpx`.

The token belongs in the client's own config or environment. It is never
committed, never in a frontend bundle, and never returned by any response.

## 4. Protocol

| | |
| --- | --- |
| Transport | stdio, one JSON-RPC message per line |
| Protocol version | `2024-11-05` |
| Server name | `lead-engine` |
| Capabilities | `{"tools": {"listChanged": false}}` |
| Methods | `initialize`, `notifications/initialized`, `tools/list`, `tools/call`, `ping` |

The server sends `instructions` on `initialize` telling the client how to
read what it gets back: call `get_health` first, do not act on a snapshot
whose `quality` is not `ok`, and treat pressure and break scores as model
scores unless the response's `calibration.kind` says `PROBABILITY`.

`server.handle(message)` is a pure function of the request, which is why
the protocol behaviour is testable without a subprocess, a socket or a
running engine.

## 5. Tools

Sixteen, all reads. `timeframe` accepts `1m 3m 5m 15m 30m 1h 4h 1d`.

| Tool | Arguments | Returns |
| --- | --- | --- |
| `get_lead_engine_status` | — | Whether the engine is running, its stream statistics, and every symbol's headline state. |
| `get_symbols` | — | The instruments the engine is subscribed to, and the timeframes it can serve candles for. |
| `get_health` | — | Feed freshness for every symbol: WS latency, book age, trade age, OI age, processing time, drops and reconnects. |
| `get_market_snapshot` | `symbol` | EVERYTHING current for one symbol in one call: price, the five layer scores, microstructure, open interest, liquidations, SMC, Elliott, levels, active signal, and data freshness. |
| `get_multi_tf_snapshot` | `symbol` | 4h/1h/15m/5m/1m context in one call — OHLC summary, EMA 9/18/50/200, swing highs and lows, BOS/CHoCH, support and resistance, and whether each timeframe's current candle is LIVE or CLOSED — plus the live microstructure. |
| `get_orderbook_state` | `symbol` | L2 book state: best bid/ask, spread, microprice, OBI at 1/5/10/25/50, weighted OBI, book alignment, pulling and replenishment, and classified walls. |
| `get_trade_flow` | `symbol` | Aggressive flow: normalised delta over eight windows, CVD and its divergence against price, trade velocity and its z-score, large prints. |
| `get_derivatives_state` | `symbol` | Open interest with its four-way price/OI interpretation, liquidation windows and flush state, funding when the ticker carries it. |
| `get_structure` | `symbol` | SMC state (HH/HL/LH/LL, BOS, CHoCH, sweep, FVG, order block, premium/discount) and the pre-break reading with all ten of its features. |
| `get_elliott_state` | `symbol` | The Lead Engine's own wave CONTEXT — a deterministic state machine, not the project's full wave analyser. |
| `get_pressure` | `symbol` | LONG_PRESSURE and SHORT_PRESSURE with the per-layer breakdown, the confidence, and the conflict level. |
| `get_active_signal` | `symbol` | The current signal state, its direction, the level under stress, and the break score. |
| `get_recent_signals` | `symbol`, `limit?` | The engine's recent feature history: pressure, break scores and signal state over time. |
| `get_candles` | `symbol`, `timeframe?`, `limit?` | Historical OHLCV from Bybit, oldest first; the newest row is marked `closed: false`. |
| `get_indicators` | `symbol`, `timeframe?`, `limit?` | EMA 9/18/50/200 for one symbol and timeframe, computed from the same closes the chart draws. |
| `get_fibonacci_levels` | `symbol`, `timeframe?` | Saved Fibonacci retracements for one symbol AND timeframe, with every level priced. |

### Which one to call

- One instrument, right now → `get_market_snapshot`. One round trip, one
  coherent moment. Calling eight slice tools instead gives eight slightly
  different ticks.
- "Where are we on the higher timeframes?" → `get_multi_tf_snapshot`.
- Before trusting anything → `get_health`.

## 6. What a tool returns

Content is a single `text` block holding compact JSON — the API response
passed through unchanged, so the field names in [`API.md`](API.md) are
exactly what arrives.

Every payload carries freshness:

```jsonc
{
  "server_time": 1789491439926,
  "data_age_ms": 39.0,
  "book_age_ms": 39.0,
  "trade_age_ms": 39.0,
  "quality": "ok",            // ok | degraded | stale
  "signals_valid": true,
  "engine_running": true,
  "read_only": true,
  "api_version": "v1"
}
```

**`signals_valid: false` means do not act on it.** The engine disables its
own signals on stale data rather than scoring old numbers, and says so
here rather than hiding it.

## 7. Errors

An error comes back as a normal tool result with `isError: true` and a
readable explanation, not as a JSON-RPC transport error — an AI client can
then say what is wrong instead of failing opaquely.

| Situation | What the client sees |
| --- | --- |
| Engine unreachable | "Cannot reach the Lead Engine at … Is it running, and is `LEAD_ENGINE_API_URL` pointing at it?" |
| `401` | "Set `LEAD_ENGINE_API_KEY` to the deployment's read-only token." |
| `404` on the namespace | External access is not enabled on that deployment (`EXTERNAL_AI_ACCESS_ENABLED`). |
| `503` | The deployment has no `LEAD_ENGINE_API_KEY` set, so it refuses every request. |
| `429` | Over the rate limit (10/s sustained, burst 30/10s). Back off and retry. |
| Unknown tool | The error names the tool and lists the valid ones. |

## 8. Streaming

MCP has no streaming tool call, so a client that needs continuous updates
should use the SSE endpoint directly:

```
GET /api/v1/lead-engine/stream?symbol=INJUSDT&interval_ms=500
```

It sends compact deltas — only the fields that changed — rather than the
whole state. See [`API.md`](API.md) §6.

## 9. Tests

`tests/test_lead_engine_external.py` covers the handshake, the tool list,
a tool call through a fake HTTP client, error handling, the stdio pipe
end to end, and the AST check that `lead_engine_mcp` never imports
`t3_engine`.
