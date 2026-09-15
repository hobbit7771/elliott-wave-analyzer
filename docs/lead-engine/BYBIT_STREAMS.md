# Bybit streams

**Bybit only.** There is no Binance path in this engine, no fallback to
one, and none is to be added. Binance is blocked in this deployment — the
project moved off it after a live IP ban and a public socket that
completed its handshake and then delivered zero trades — so a fallback
that cannot connect is not resilience, it is a second way to fail. A test
asserts the absence per file.

## Endpoint

```
wss://stream.bybit.com/v5/public/linear
```

Overridable with `T3_LEAD_ENGINE_WS_URL` (used only for testing against a
local fake). REST base: `https://api.bybit.com`, overridable with
`T3_LEAD_ENGINE_REST_BASE`.

## Topics, per symbol

| Topic | Carries | Used by |
|---|---|---|
| `orderbook.50.{symbol}` | snapshot + deltas, 50 levels | `orderbook_engine`, `microprice`, `prebreak_engine` |
| `publicTrade.{symbol}` | every print, with the **taker's** side | `trade_flow`, `cvd`, `candles`, `btc_leadlag`, `orderbook_engine.note_trade` |
| `tickers.{symbol}` | last price, funding, 24h stats | header, `health` |
| `allLiquidation.{symbol}` | forced closes | `liquidation_engine` |
| `kline.{1,5,15,60,240}.{symbol}` | candles | `smc_engine`, `elliott_state` |

Subscriptions are sent in chunks of ten arguments per request, which is
Bybit's documented safe size.

## Default symbols

`BTCUSDT, INJUSDT, DOGEUSDT, AAVEUSDT, ATOMUSDT, FILUSDT, NEARUSDT` —
configurable with `T3_LEAD_ENGINE_SYMBOLS` (comma separated).

**BTCUSDT is always subscribed**, whatever the list says. It is not a
preference: "did BTC move first" is unanswerable without a continuous BTC
stream, so `config.symbols()` inserts it if it is missing.

## Two schema details that are easy to get backwards

**`publicTrade.S` is the TAKER's side.** `"Buy"` means someone lifted the
offer. This is *not* inverted the way Binance's aggTrade `m` flag is, and
`engine.py` carries a comment saying so — flipping it would invert CVD,
delta, every flow reading and the sign of the pressure score.

**`allLiquidation.S` is the ORDER's side, not the position's.** Closing a
long means selling, so `"Sell"` is a **long** being liquidated. The
mapping lives in one named function, `side_is_long_liquidation`, with a
test, because inverting it inverts `LONG_FLUSH`, `SHORT_SQUEEZE` and the
sign of the liquidation component.

## Open interest

Not a WebSocket topic. Polled from `GET /v5/market/open-interest`
(`category=linear`, `intervalTime=5min`) on a **separate thread**, every
`T3_LEAD_ENGINE_OI_POLL_SECONDS` (default 60). It never runs on the socket
loop: a REST endpoint that hangs must not hold up the order book.

Because Bybit reports OI on a five-minute grid, it is treated as context,
never as a tick. Stale OI costs the engine one component out of nine — it
is a note on the health panel, not a degradation.

## Reconnect

Exponential backoff from 1s to a 30s cap, with a ping every 20s to keep an
idle connection from being closed. `connects`, `reconnects`, `messages`,
`last_error` and a **median** latency (exchange stamp vs local clock;
includes clock skew and is reported as a measurement, not a guarantee) are
all on `/api/lead-engine/status`.
