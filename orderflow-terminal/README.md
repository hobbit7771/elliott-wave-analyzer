# OrderFlow Terminal

A browser-based order-flow trading terminal: candlestick chart (TradingView lightweight-charts), order-book heatmap, DOM, footprint, CVD/Delta, Volume Profile/TPO and a mathematical detector for large orders, probable icebergs, absorption, spoofing (as suspicion), clusters, sweeps, stop runs and imbalances.

**Analysis, paper trading and backtesting only.** The project has no code that sends orders to an exchange and needs no API keys. It uses only public, free endpoints.

> **Honest boundary.** The public order book never shows the hidden part of an iceberg order. The terminal shows a **Probable Iceberg / Estimated Hidden Liquidity / Iceberg Confidence** estimate, never a fact. Every event carries a confidence score (0–100) and a text explanation of why it was detected.

---

## Contents
1. [Stack and why it was chosen](#stack)
2. [Project file tree](#tree)
3. [System architecture](#arch)
4. [Implemented features](#features)
5. [Detector algorithms](#algo)
6. [Data source limitations](#limits)
7. [Local setup](#local)
8. [Deploying on Render Free](#render)
9. [Tests](#tests)
10. [Live WebSocket verification](#verify)
11. [Performance report](#perf)
12. [Features deliberately not claimed](#notclaimed)
13. [Moving to a separate repository](#split)

---

<a id="stack"></a>
## 1. Stack and why it was chosen

| Layer | Choice | Why |
|---|---|---|
| Language | TypeScript everywhere | One core (book, detectors, footprint, paper) is shared by the server, the browser, the replay tool and the tests, so the same math runs everywhere |
| Server | Node.js 22, `http` + `ws`, `worker_threads` | No framework: minimal RAM for Render Free (512 MB). Ingestion for each instrument runs in its own worker thread, separate from the HTTP/WS thread |
| Database | SQLite via built-in `node:sqlite` (WAL) | No native dependencies, nothing to install, free. Retention tiers keep the size bounded |
| Chart | `lightweight-charts` v5 (TradingView, Apache-2.0) | The real TradingView engine: scaling, history scrolling, crosshair, panes, price-anchored markers |
| Heatmap / DOM / Footprint / Profile | Canvas 2D, custom renderers | Full control over performance (ImageData, redrawing only visible rows/columns, rAF throttling) |
| Client networking | Web Worker (`net.worker.ts`) | WebSocket and JSON parsing stay off the UI thread; batching and coalescing provide backpressure |
| Build | Vite (web) + esbuild (server) | Fast, single-file server bundle |
| Tests | Vitest + Playwright (Chromium) | Unit, integration (real local WebSockets), load, e2e on iPhone and desktop viewports |

<a id="tree"></a>
## 2. Project file tree

```
orderflow-terminal/
├── Dockerfile, .dockerignore, render.yaml, .node-version, .env.example
├── package.json, tsconfig.json, vite.config.ts, vitest.config.ts, vitest.e2e.config.ts
├── scripts/
│   ├── build-server.mjs          # esbuild: server, worker, tools
│   └── dev.mjs                   # dev: rebuild + restart server, Vite dev server
├── src/
│   ├── core/                     # pure logic, no IO (shared by server/client/tests)
│   │   ├── types.ts              # unified format: Trade, DepthDiff, Candle, HeatColumn, MarketEvent...
│   │   ├── precision.ts          # tick grid, price/quantity precision
│   │   ├── orderbook.ts          # OrderBook, BookSync (sequences), FlowClassifier (add/cancel/execute), OFI
│   │   ├── candles.ts            # timeframes tick/1s/1m…1D, candles from trades
│   │   ├── indicators.ts         # EMA, RMA, ATR, RSI, MACD, VWAP, CVD
│   │   ├── footprint.ts          # footprint, imbalance, stacked, POC, UA, Volume Profile, HVN/LVN, TPO
│   │   ├── heatmap.ts            # heatmap columns from the local book, merging, CSV
│   │   ├── engine.ts             # MarketEngine: book → flow → detectors → heatmap
│   │   ├── paper.ts              # paper trading + event backtests
│   │   ├── replay.ts             # deterministic replay of NDJSON recordings
│   │   ├── stats.ts              # rolling sums, percentiles, EWMA
│   │   └── detectors/            # config, context, largeOrders (+spoof, pulled), iceberg, flow, clusters
│   ├── server/
│   │   ├── index.ts              # HTTP API, static files, WebSocket, production guards
│   │   ├── hub.ts                # sessions per instrument, fan-out, backpressure
│   │   ├── session.ts            # WS ingestion, snapshot/resync, stale gating, BBO check
│   │   ├── worker.ts             # worker thread for one instrument
│   │   ├── recorder.ts           # SQLite: trades, heat (1s/10s/60s), events, candles, settings
│   │   ├── ingestion/wsClient.ts # reconnect, heartbeat, backoff
│   │   └── adapters/             # MarketAdapter, Binance Futures/Spot, registry (+ unavailable sources)
│   ├── tools/                    # verify-live, record (NDJSON), replay
│   └── web/                      # frontend
│       ├── index.html, styles.css, main.ts, store.ts, util.ts, net.worker.ts
│       ├── public/ (manifest, sw.js for notifications, icon)
│       └── tabs/ chart, heatmap, dom, footprint, profile, signals, paper, alerts, sources
└── tests/
    ├── fixtures/ sim.ts (deterministic exchange simulator), fakeVenue.ts   ← tests only
    ├── e2e/ testVenue.ts (local Binance-protocol venue), ui.e2e.ts, perf.e2e.ts
    └── *.test.ts                 # 84 unit/integration tests
```

<a id="arch"></a>
## 3. System architecture

```
 Binance WS (depth@100ms, aggTrade, bookTicker, markPrice@1s, forceOrder)      Binance REST
            │                                                                   (depth snapshot,
            ▼                                                                    klines, aggTrades,
 ┌───────────────────────── worker thread (one per instrument) ─────────────┐     exchangeInfo, OI)
 │ 1 Ingestion   ReconnectingWs: heartbeat (20 s silence), ping,            │◄──────────┘
 │               exp. backoff, 23 h reconnect, buffer ≤2000 diffs           │
 │ 2 Normalize   adapter.parse → Trade / DepthDiff / BBO / Mark / Liq (UTC)  │
 │ 3 Book engine BookSync (U/u/pu) → OrderBook → FlowClassifier + OFI        │
 │               gap → Data gap → snapshot resync; BBO cross-check           │
 │ 5 Detection   MarketEngine: large/iceberg/absorption/spoof/cluster/...    │
 │               gate: no signals when stale / unsynced / disconnected       │
 │ 4 Recorder    SQLite: trades, heat 1s→10s→60s, events, 1m candles         │
 └───────────────┬──────────────────────────────────────────────────────────┘
                 │ postMessage (pre-serialized JSON)
 ┌───────────────▼────────── main thread ───────────────────────────────────┐
 │ Hub: subscriptions, cache of latest book/status, backpressure             │
 │   (>1 MiB buffer → drop book/heat and send a trade "gap" → REST reload;   │
 │    >8 MiB → disconnect)                                                   │
 │ REST: /api/klines /trades /heatmap /events /config /perf /export/*        │
 └───────────────┬──────────────────────────────────────────────────────────┘
                 │ WebSocket /ws
 ┌───────────────▼────────── browser ───────────────────────────────────────┐
 │ Web Worker: WS + JSON parse + batches every 100 ms (ack backpressure)     │
 │ Store → tabs (6 Visualization): Chart, Heatmap, DOM, Footprint,           │
 │   Profile/TPO, Signals; 7 Paper/Backtest; Alerts; Sources & Settings      │
 │ Rendering: requestAnimationFrame + per-tab throttling, only visible tabs  │
 └───────────────────────────────────────────────────────────────────────────┘
```

Adapters: the `MarketAdapter` interface (`src/server/adapters/adapter.ts`) declares only capabilities that actually exist (`caps`) plus a list of `limitations`. Implemented: **Binance USDⓈ-M Futures** and **Binance Spot**. Bybit / CME / CFD are listed as unavailable with the reason; they never appear in the instrument picker.

<a id="features"></a>
## 4. Implemented features

**Data and infrastructure**
- Binance USDⓈ-M Futures: depth diff 100 ms, aggTrade, bookTicker, markPrice + funding, open interest (REST every 15 s), liquidations (forceOrder), klines, depth snapshot of 1000 levels. Binance Spot: depth, aggTrade, bookTicker, klines (including native 1s).
- Local book: snapshot + buffered diffs using Binance rules (futures `pu == prev u`, spot `U == prev u + 1`), automatic resync on a gap, cross-check against bookTicker (a crossed book for >3 s triggers a forced resync), pruning of distant levels, a "reliable range" (the heatmap does not show unknown zones as empty).
- Stale-data protection: no depth for more than `staleMs` (5 s) puts the feed in `Stale` and closes the gate on **all** new signals; 30 s → forced reconnect.
- Statuses: Connected / Reconnecting / Stale / Snapshot syncing / Data gap / Disconnected, last update, latency (exchange→server EWMA), RTT (browser↔server), trade count, depth levels, gaps/resyncs, dropped (server/browser), source, freshness indicator.
- History: SQLite with retention (heat 1s: 2 h, 10s: 24 h, 60s: 7 d; trades: 6 h; events: 7 d), replay, CSV/JSON/PNG export, history purge.

**Tabs**
- **Chart**: candles (tick with a choice of 50–1000 trades, 1s, 1m, 3m, 5m, 15m, 30m, 1H, 4H, 1D), zoom/scroll with lazy loading of older history, crosshair, volume, EMA 9/18/50/200, VWAP, ATR, RSI, MACD, CVD, Delta; large-order price lines with confidence; markers for icebergs, sweeps, stop runs, imbalance, divergence, bursts, liquidity removal and spoofing suspicion anchored to their **price and time**; absorption/cluster/vacuum zones; user trading levels and paper SL/TP; liquidations; per-layer toggles and a minimum-confidence filter.
- **Heatmap**: time × price axes, bid/ask history from real snapshots of the local book (1 column/s), colours: cold (weak) → yellow/orange (elevated) → red (large ask) / green (large bid), purple for probable icebergs, white/grey for removed liquidity (cancels); added liquidity; executions (bubbles); held (Held) / pulled / filled / broken large orders; clusters (Bid/Ask/Absorption/Tested/Broken); absorption zones; liquidity vacuum; price line or candles, bid/ask lines; depth range, history period (1m…7d), minimum volume, minimum confidence, intensity, log scale, pause, replay (1–60×), view clear, CSV and PNG export, crosshair tooltip.
- **DOM**: bid/ask, price, displayed quantity, executed at bid/ask over the period, delta, volume, imbalance, last trade, spread, microprice/mid, book imbalance, OFI, trade speed, session CVD, large-order threshold; highlighting of active levels, large levels, `ICE`/`ABS`. Settings: grouping, number of levels, minimum large size, minimum refills (writes to the server detector), analysis period, colour scheme. Canvas virtualization.
- **Footprint**: bid × ask / delta / volume, buy/sell diagonal imbalance (ratio, minimum volume), stacked imbalance, POC, HVN/LVN (visible-range profile), unfinished auction, absorption/iceberg markers, bar delta, session delta (UTC), volume.
- **Profile / TPO**: exact Volume Profile from trades (POC/VAH/VAL/HVN/LVN, split into buys and sells), TPO built from 30m candles (letters, POC, VA, IB).
- **Signals**: event journal (type, side, price, confidence, status, explanation), large-order table (price, size, side, appearance time, hold duration, refills, executed, cancelled, status, confidence, source), clusters, iceberg candidates, JSON export.
- **Paper**: market paper orders against the live book (opposite side of the spread + slippage), SL/TP, fees, funding at the exchange's funding-time rollover, P&L, win rate, expectancy, profit factor, max drawdown, journal; backtest of detector events on recorded trades.
- **Alerts**: rules (instrument, event types, minimum confidence, minimum volume, timeframe = at most one alert per bar, sound, browser notification); feed events (disconnect, stale, resync, data gap, spread expansion) plus loss of the browser↔server connection.
- **Sources & Settings**: available/unavailable sources and their limitations, a per-instrument editor for all detector parameters, server performance.

<a id="algo"></a>
## 5. Detector algorithms (all math runs on WebSocket data; no LLMs)

**Add / cancel / execute classification** (`FlowClassifier`). An aggressive sell (`m=true`) executes against the bid at the trade price; a buy executes against the ask. For each diff at time T and each level: `drop = max(0, old−new)`, `executed = min(drop, V≤T)`, `cancelled = drop − executed`, `added = max(0, new−old)`, `hidden = V − executed` (volume that the visible depletion does not explain). A trade that arrives after its diff reclassifies the level's recent cancel as an execution.

**Large limit order.** `threshold = max(minQty, depthPct × side depth within ±rangePct, P97 of level sizes sampled once per second) × ATR factor` (`sqrt(ATR/avgATR)` clamped to [1, 1.5]). A level must exceed the threshold, sit at least `minDistanceTicks` from mid and hold for at least `minHoldMs`. Warm-up: no signals until 400 samples exist. Tracked: size, peak, appearance, hold time, refills, executed, cancelled, status (active/partially_filled/filled/pulled/broken), confidence.

**Probable Iceberg.** For each level being hit, the detector accumulates displayed size, executed volume, the executed volume the depletion does not explain, refills (both "executed without depletion" and "size added right after execution"), time at best, trade-throughs, the share of opposite-side aggression hitting the level, CVD against the level, the share of time with OFI/microprice against the level, trade speed and cancel ratio. **Eligibility** (all required): `refills ≥ minRefills` (3), `traded/maxDisplayed ≥ 1.5`, observation ≥ 4 s, the level is not broken, `cancel ratio ≤ 0.6`, the level is not on the spoof list, and `Estimated Hidden = traded − displayed at first hit > 0`. **Confidence** = 100 × (0.25·ratio + 0.20·refills + 0.12·hold + 0.15·at-touch + 0.15·(concentration+CVD) + 0.05·speed + 0.08·(OFI+microprice)) × (1 − cancelRatio). **Types:** absorption iceberg, replenishment iceberg, probable bid/ask iceberg, weak suspicion (<45). When price trades through the level, status becomes `broken`.

**Absorption.** Over a 10 s window: aggressive volume ≥ P95 of the rolling distribution, adverse move ≤ max(2 ticks, 0.15·ATR1m), no new extreme for ≥ 3 s, and ≥ 50 % of the volume hit the extreme zone after it formed.

**Spoofing suspicion.** A large order that lived ≤ 20 s, was ≥ 80 % cancelled with ≤ 10 % executed, and was pulled as price approached **or** repeated appear/cancel cycles (≥ 3 in 2 min). This is a suspicion only; the level is excluded from iceberg detection.

**Clusters / vacuum.** Buckets of `zoneStep` within ±1 %: elevated when ≥ 2.5 × the median bucket, clusters of ≥ 3 buckets with gaps ≤ 1. Tracked over time with statuses formed/tested/absorption/broken. Vacuum: ≥ 5 consecutive buckets holding ≤ 15 % of the median, starting from best price.

**Sweep / Stop run.** Same-side trades within 1 s crossing ≥ max(5 ticks, 0.2·ATR) with volume ≥ P95; the event keeps extending while the sweep continues. Stop run: a sweep beyond the 30-bar 1m extreme followed by a return behind that level within 60 s.

**Imbalance** (|OBI top-10| ≥ 0.6 held for ≥ 3 s), **Volume burst** (1 s volume ≥ P99.5 and ≥ 5× the median), **Delta divergence** (new 10-bar high/low without CVD confirmation), **Spread expansion** (≥ 4× the median for ≥ 1 s). OFI follows Cont–Kukanov–Stoikov; microprice = (bid·askQty + ask·bidQty)/(bidQty + askQty).

Every parameter can be edited per instrument (Sources & Settings → saved on the server and applied to the live detector immediately).

<a id="limits"></a>
## 6. Data source limitations

- **Hidden orders are not visible** in public data. An iceberg can only be estimated. Displayed size is available only in aggregate per price level (no individual orders, no MBO).
- Binance depth diffs are batched every **100 ms**: adds and cancels within one batch are merged, and the classification is statistical.
- The trades stream and the depth stream are separate: trades are joined to diffs by time, and a late trade triggers reclassification (this is still not an exact queue).
- Heatmap history exists **only from the moment the server started recording**. The exchange provides no historical order book.
- Futures REST has no 1s candles: 1s/tick charts and the footprint are built from recorded trades plus an aggTrades backfill (10 minutes by default). For 1H–1D the footprint covers only the recorded range, and the tab shows which range that is.
- `forceOrder`: at most one liquidation per symbol per second (a Binance rule). Open interest is REST-only.
- **Binance Futures blocks US IPs (HTTP 451).** Deploy in the EU or Asia (Frankfurt/Singapore).
- Render Free: the disk is **ephemeral**; after a restart or sleep, history is lost. The service sleeps after 15 minutes without HTTP traffic, and ingestion stops while it sleeps.
- **CME (NQ, ES, GC, CL)**: a real DOM requires a paid licensed feed (Rithmic/CQG/dxFeed/Databento), so it is not offered. **XAUUSD / BRXUSD CFD**: no centralized order book exists (a broker's quotes are not an exchange book), so they are not offered. If Binance lists a commodity perpetual (e.g. XAUUSDT), the UI warns that this is **Binance's own order book**, not CME/COMEX/OTC. Data from different sources is never mixed.
- Alerts and paper stops fire while the instrument is open in the browser (no server-side Web Push).

<a id="local"></a>
## 7. Local setup

Requirements: Node.js ≥ 22.12, and network access to `fapi.binance.com`, `fstream.binance.com` (plus `api.binance.com` and `stream.binance.com` for Spot).

```bash
cd orderflow-terminal
npm ci
npm run build
npm start                     # http://localhost:8080
# or in development mode (Vite on :5173 with a proxy to :8080):
npm run dev
```

Settings go in `.env` variables (see `.env.example`); none are required.

<a id="render"></a>
## 8. Deploying on Render Free

**Option A: Blueprint.** When the project is in its own repository (see §13), `render.yaml` is at the repository root: Render → New → Blueprint → choose the repository → Apply.

**Option B: manual setup** (also works from a subfolder of this repository):
1. Render → New → Web Service → connect the repository.
2. **Root Directory:** `orderflow-terminal`
3. **Runtime:** Node · **Region:** Frankfurt (not US, because of Binance's HTTP 451) · **Instance type:** Free
4. **Build Command:** `npm ci --include=dev && npm run build`
5. **Start Command:** `npm start`
6. **Health Check Path:** `/api/health`
7. Environment: `NODE_ENV=production`, `NODE_VERSION=22.12.0`, `NODE_OPTIONS=--max-old-space-size=384`, `MAX_SESSIONS=2`, `DB_PATH=/tmp/orderflow.sqlite`.

**Docker** (Render/Fly/Koyeb/any host): `docker build -t orderflow-terminal . && docker run -p 8080:8080 -v oft-data:/data orderflow-terminal`.

On iPhone: open the site in Safari → Share → "Add to Home Screen" (notifications require iOS 16.4+ and the app installed to the home screen).

<a id="tests"></a>
## 9. Tests

```bash
npm test              # 84 unit/integration tests (vitest)
npm run test:load     # load test of the engine only
npm run test:e2e      # build + real server + Chromium (iPhone 390×844 and desktop 1440×900) + performance
npm run typecheck
```

Coverage: order-book reconstruction, sequence gap/resync (futures and spot), add/cancel/execute classification and late trades, OFI; iceberg detector (positive, ordinary large order, too little data, trade-through, closed gate); large-order detector (warm-up, threshold, lifecycle, pulled); spoofing filter; clusters (formed/tested/broken); absorption; sweep/stop run; imbalance/burst/divergence; heatmap aggregation/merging/tiers/CSV; candles/indicators/footprint/VP/TPO; paper trading and backtests; WebSocket reconnect, heartbeat and backoff; the full live session over a local WS (sync, gap→resync, stale→gate closed→recovery, reconnect); recorder and retention; deterministic replay of recordings; load test; **checks that production code contains no fake data** (no `Math.random`, no imports of fixtures/mocks, no hard-coded price series; in production the server refuses to start with non-Binance endpoints); e2e mobile layout (no horizontal overflow, all tabs, heatmap/DOM/footprint render, a paper order).

Synthetic scenarios (`tests/fixtures/sim.ts`, `tests/e2e/testVenue.ts`) exist **only in `tests/`**; the `no-fake-data` test guarantees that `src/` never imports them.

<a id="verify"></a>
## 10. Live WebSocket verification

```bash
# directly against Binance: snapshot + diffs, sequences, comparison with bookTicker, latency
npm run verify:live -- --symbol BTCUSDT --seconds 20
npm run verify:live -- --source binance-spot --symbol ETHUSDT

# a deployed server: health, klines, WS subscription, book, trades, status
npm run verify:live -- --server https://<your-app>.onrender.com --symbol BTCUSDT

# record a live stream and replay it through the detectors
npm run record -- --symbol BTCUSDT --seconds 120 --out recordings/btc.ndjson
npm run replay -- recordings/btc.ndjson

# manually
curl "https://<app>/api/klines?source=binance-futures&symbol=BTCUSDT&tf=1m&limit=5"
curl "https://<app>/api/perf"
npx wscat -c wss://<app>/ws   # then: {"op":"sub","source":"binance-futures","symbol":"BTCUSDT"}
```

If Binance changes its WebSocket addresses, override `BINANCE_FUTURES_WS` / `BINANCE_FUTURES_REST` (in production only official `*.binance.com` / `*.binance.vision` hosts are accepted).

<a id="perf"></a>
## 11. Performance report (measured)

| Metric | Result | Where measured |
|---|---|---|
| Engine throughput (book + classification + all detectors + heatmap) | **~88–96k msg/s** (110k messages in ~1.2 s), heap +17–21 MB | `tests/load.test.ts` |
| Server event-loop lag with 1 instrument, 3 WS clients, recording on | p50 **0.17 ms**, p99 **1.0 ms**, max 4.7 ms | `tests/e2e/perf.e2e.ts` |
| Server RSS (main + worker) | **~109 MB** under load, ~79 MB idle | same |
| `/api/heatmap` query for 15 minutes | **~55 ms** | same |
| Sequence gaps on a continuous stream | 0 | same |
| Frontend bundle | 276 KB JS (93 KB gzip) + 6 KB CSS | `vite build` |

UI measures: rendering only in the visible tab, rAF throttling (heatmap ≤ 10 FPS, DOM ≤ 10 FPS, footprint ≈ 8 FPS), coalescing of book/status in the Web Worker, ack-based backpressure, caps on candles (20k), trades (400k), heatmap columns (8k) and events (4k), and the heatmap drawn through a single ImageData at CSS resolution.

Note: the numbers above were measured on a local test venue (10 diffs/s) and in the in-process load test. Real BTCUSDT carries more levels per diff; the ~90k msg/s engine capacity leaves a large margin, but the 0.1 CPU on Render Free is the main limit, so keep `MAX_SESSIONS=2`.

<a id="notclaimed"></a>
## 12. Features deliberately not claimed

- "Seeing" hidden/iceberg orders: only a probable estimate with a confidence score.
- Proof of spoofing: only "Possible Spoofing (suspicion)".
- MBO / queue position / individual orders: Binance publishes only aggregated levels.
- Order-book history from before the server started recording: the exchange provides none.
- CME (NQ, ES, GC, CL) data, and CFD XAUUSD/BRXUSD order books: no free public source exists.
- Bybit: adapter interface only, not implemented in this release.
- Real trading: no order-sending code exists, by design.
- Server-side push notifications when the browser is closed: not implemented.
- Persistent history on Render Free: the disk is ephemeral.

<a id="split"></a>
## 13. Moving to a separate repository

The project is fully self-contained in the `orderflow-terminal/` folder and depends on nothing outside it:

```bash
git subtree split --prefix=orderflow-terminal -b orderflow-terminal-only
git push git@github.com:<you>/orderflow-terminal.git orderflow-terminal-only:main
```
