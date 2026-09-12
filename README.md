# elliott-wave-analyzer

Инструмент для анализа волн Эллиота с подключением к Binance, расчётом Фибоначчи и визуализацией.

> The root-level `app.py` / `templates/index.html` is an earlier, simpler
> prototype (single-timeframe REST analysis + matplotlib chart). It
> currently imports `binance_connector.py`, `elliott_wave_analyzer.py`,
> `fibonacci_calculator.py`, `visualizer.py` - none of those files exist in
> this repository, so `app.py` does not run as-is; that predates this work
> and was left untouched since fixing the old prototype was not part of
> this task. **The real deliverable is `t3_engine/`, described below.**

---

# T3 — Multi-Timeframe Elliott Wave Trading Engine

A modular, testable, mostly-real implementation of the T3 spec: a
multi-timeframe Elliott Wave analysis and (paper-)trading engine for
Binance USDT-M Futures. 108 automated tests, all passing, cover every
module described below.

## Read this first: what "done" means here

This spec describes a multi-month production trading system (live
exchange execution, TimescaleDB, a TradingView-grade UI, 1000+-trade
walk-forward validation across multiple assets). That is not something
one build session can honestly deliver as certified, production-ready,
real-money software - and this README says exactly where the line is,
per the spec's own section 30 ("if something can't be delivered
literally, explain the technical reason and propose the nearest correct
solution").

**What is genuinely implemented, working and tested:** the full analysis
and decision pipeline - causal candle aggregation, ZigZag pivot detection,
BOS/CHoCH/liquidity-sweep market structure, hard Elliott rule validation
(impulse/diagonal), corrective structure classification, a probabilistic
top-3 scenario engine, the section-8 wave state machine, the section-21
entry-timing state machine, Fibonacci relationship scoring, momentum/
order-flow/derivatives scoring, the section-23 weighted confidence score,
wave-specific risk management and TP/SL construction, a PAPER execution
engine + position manager (partial TPs, structural trailing stop, MAE/MFE),
a SQLAlchemy database layer, an event-driven backtester with a proven
no-lookahead guarantee (see `tests/test_backtest.py`), and a FastAPI +
TradingView-lightweight-charts dashboard that draws real candles with real
overlays.

**What is real code but not network-verified in this build:** the Binance
REST and WebSocket clients (correct against the documented API schema,
unit-tested against mocked responses/fake sockets) and the
Testnet/Live execution scaffold (correct HMAC request signing and order
payload building, unit-tested offline). See "Known limitations" below for
exactly why, and the 3-step path to activate each once you have network
access.

**What is explicitly a documented simplification, not a hidden gap:** see
"Known limitations" - sub-minute historical backtesting, corrective-wave
classification, and single-timeframe entry timing in the backtester.

## Architecture

```
t3_engine/
  common/            shared enums + dataclasses (Candle, Wave, Scenario, Signal, Position, Order...)
  config/            pydantic Settings, all knobs environment-configurable
  candle_builder/     causal trade -> candle aggregation for every TF (1s...4h) + resampling
  market_structure/   ZigZag pivot detector, BOS/CHoCH, liquidity sweeps
  fibonacci/          retracement/extension calculators (waves 2/3/4/5/C)
  elliott_engine/     hard impulse/diagonal rules, correction classification,
                      probabilistic top-3 scenario engine, section-8 state machine
  orderflow/          taker flow, absorption, divergence, EMA/MACD/ADX
  derivatives/        OI / funding / long-short-ratio tracker
  signal_engine/      section-21 entry-timing state machine, section-23 confidence
                      scoring, wave-specific TP/SL target construction
  risk_engine/        position sizing, drawdown breakers, correlated-exposure cap
  execution/          ExecutionEngine interface, PAPER simulator, Binance live/testnet scaffold
  position_manager/   position lifecycle, partial TPs, structural trailing stop, MAE/MFE
  database/           SQLAlchemy ORM + schema.sql (raw_trades, candles, wave_states,
                      wave_scenarios, signals, orders, positions, backtest_results)
  market_data/        Binance USDT-M Futures REST + WebSocket clients
  backtest/           event-driven, no-lookahead backtester + metrics + synthetic fixture
  pipeline/           live_loop.py - the section-20 event-driven real-time orchestrator
  dashboard/           FastAPI backend + static/index.html (lightweight-charts UI),
                      PWA manifest/service worker, live-pipeline start/stop/state endpoints
  ai_advisor/         optional BYO-key GPT second-opinion commentary (never a decision-maker)
  logger/             JSON-lines decision journal (SIGNAL_ACCEPTED/REJECTED + full context)

tests/                108 tests, one file per module above
run_backtest.py        CLI: run a backtest, print a metrics report
run_paper_trading.py   CLI: run the live pipeline against Binance in PAPER mode
run_dashboard.py       CLI: serve the dashboard
```

Every module has a docstring explaining *why* it's structured the way it
is, not just what it does - read those before changing the hard-rule or
no-lookahead logic in particular.

## Mobile app + live Binance + AI advisor (dashboard)

`dashboard/static/index.html` is a mobile-installable PWA, not just a
desktop web page:

- **"Add to Home Screen"** on iOS Safari or Android Chrome installs it as
  an app icon (manifest + service worker in `dashboard/static/`), no App
  Store/Google Play submission needed - that's the fast/simple path to a
  "mobile app" versus building and shipping a separate native app.
- **Live public WebSocket mode**: pick "Binance (live, public WS)" as the
  source, enter a symbol in plain Binance format (e.g. `BTCUSDT` - no
  slash, no quote-currency separator: `UNI/USDC` or `SOL/USDT` are rejected/
  normalized, since Binance symbols are just one alphanumeric string) and
  hit "Start live". This calls `POST /api/live/start`, which spins up the
  real `pipeline/live_loop.py` orchestrator against Binance's **public**
  market-data WebSocket (aggTrade/bookTicker/markPrice) for that symbol -
  no API key required, since market data isn't account data.
  **Binance blocks entire regions from its API with HTTP 451** (confirmed
  in production logs of a Render deployment of this exact app, hosted in
  Render's default `oregon` (US) region) - this is Binance's own
  regulatory IP block, not a bug here, and it affects the REST history
  endpoint the same way it affects the live WebSocket. If "Binance
  (history)" or "Binance (live)" don't return data, redeploy this service
  in a **non-US Render region** (`frankfurt` or `singapore` - see
  `render.yaml`'s `region` field, or the region picker when creating the
  service manually); this build's own sandbox separately blocks Binance
  entirely regardless of region (see "Known limitations" above).
- **Symbol picker**: the symbol field is backed by `GET /api/symbols`
  (cached in-process for an hour), which lists every actively-tradeable
  Binance USDT-M perpetual futures symbol via `/fapi/v1/exchangeInfo` -
  type to filter instead of guessing a ticker format. This is best-effort:
  if it can't reach Binance (451-blocked region, or the free-tier instance
  is still cold-starting), the field just falls back to plain text entry.
- **Render free-tier cold starts**: a free Render web service spins down
  after ~15 minutes with no HTTP traffic and takes up to roughly a minute
  to wake back up on the next request - this looks exactly like an
  "infinite connecting" hang if nothing tells you what's happening. Every
  dashboard request now times out after 45s and shows a message explaining
  this instead of spinning forever; if you hit it, the fix is just to wait
  and retry, not to redeploy. A paid Render plan (or pinging the service
  periodically) avoids the sleep entirely.
- **Binance HTTP 418 ("I'm a teapot")**: Binance's documented response for
  an IP that's been temporarily auto-banned for exceeding its request rate
  limit. On shared hosting (Render's free/shared plans included), your
  service's outbound IP can be shared with other tenants, so a ban can be
  inherited from traffic you never sent yourself - this was observed in
  practice on a fresh Frankfurt deploy of this app that had made exactly
  one prior Binance request. The dashboard now tracks this with a shared,
  process-wide cooldown (`server.py`'s `_binance_backoff_until`): the
  moment any endpoint sees a 418/429, every Binance-touching endpoint
  backs off for the `Retry-After` duration Binance sent (or 120s if it
  didn't send one) instead of hammering Binance again immediately, which
  is exactly what turns a short ban into a long one per Binance's own
  rate-limit rules. If this keeps recurring, a Render plan with a
  dedicated/static outbound IP address stops the ban-inheritance problem
  at the root (you stop sharing an IP with whoever triggered the ban).
- **AI Advisor tab**: paste your own OpenAI API key (your ChatGPT/OpenAI
  subscription/credits - stored only in your browser's `localStorage`,
  forwarded per-request to `/api/ai/advice` and never written to disk
  server-side, see `ai_advisor/advisor.py`). It asks GPT for a skeptical
  second opinion on the current scenario/signal in plain text. This is
  strictly advisory: GPT can never accept/reject a trade or move a stop -
  the rule-based engine already made that call before GPT ever sees it.

## Deploying to Render

1. Push this repo to your own GitHub, then in Render either:
   - **New +** → **Blueprint** → point at your repo (`render.yaml` at the
     repo root configures everything automatically), or
   - **New +** → **Web Service** → point at your repo and accept the
     defaults - Render auto-detects Python and runs
     `pip install -r requirements.txt`, which is now safe (see below).
     Just set the **Start Command** to
     `uvicorn t3_engine.dashboard.server:app --host 0.0.0.0 --port $PORT`
     if Render doesn't pick that up from `Procfile` automatically.
2. Once deployed, open the Render URL on your phone and "Add to Home
   Screen" - that's your mobile app.
3. Render's free-tier disk is **ephemeral** (wiped on every redeploy/restart),
   so the default SQLite file and `logs/` won't survive a redeploy. For
   trade history that persists, add a Render Postgres instance and set
   `T3_DATABASE_URL` to its connection string (see `.env.example`).
4. Nothing here needs a Binance API key (market data is public). If you
   later want the AI Advisor tab to work, you (or your users) just paste
   an OpenAI key into the browser - no server-side config needed for that
   either.

### Troubleshooting: "Exited with status 1 while building your code" /
### "Build aborted: the NumPy Cython headers require Cython 3.0.0 or newer"

This was caused by the root `requirements.txt` also carrying the
**legacy** `app.py` prototype's old pandas/numpy/matplotlib pins, which
Render tried to compile from source on newer Python images and failed -
the T3 dashboard never imported any of those packages in the first place.
Fixed: those pins were moved out to `requirements-legacy.txt` (kept only
in case someone wants to resurrect `app.py` later); the root
`requirements.txt` now installs cleanly on any Python version Render
picks. If you still see this error, your service is on an old commit -
go to **Manual Deploy → Deploy latest commit** (or **Clear build cache &
deploy** if that doesn't help) to pick up the fix.

### Troubleshooting: "bash: line 1: gunicorn: command not found" (build
### succeeds, deploy fails)

If this Render service was created before the T3 engine existed, Render's
auto-detect may have saved a **Start Command** of `gunicorn app:app` back
when `app.py` was a Flask prototype. That Start Command is an explicit
override stored on the service and does not update itself when
`Procfile`/`render.yaml` change - but it doesn't need to: `app.py` at the
repo root now re-exports the real T3 FastAPI app, `gunicorn.conf.py` at
the repo root tells gunicorn to use Uvicorn's ASGI worker (auto-loaded by
gunicorn with no extra flags needed), and `gunicorn` itself is in
`requirements.txt` - so the existing `gunicorn app:app` command now just
works as-is. Redeploy (**Manual Deploy → Deploy latest commit**) and it
should come up; no changes needed in the Render dashboard.

## Install & run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt   # T3 engine deps only; legacy app.py deps are in requirements-legacy.txt

# Run the automated test suite (108 tests)
pytest tests/ -q

# Run a backtest against the synthetic demo fixture (no network needed)
python run_backtest.py --cycles 3 --threshold 70

# Run a backtest against REAL Binance history (needs network access to fapi.binance.com)
python run_backtest.py --source binance --symbol BTCUSDT --limit 1500

# Serve the dashboard at http://localhost:8000
python run_dashboard.py

# Run the live event-driven pipeline in PAPER mode against real Binance
# market data (needs network access to fstream.binance.com)
python run_paper_trading.py --symbol BTCUSDT
```

Copy `.env.example` to `.env` to configure risk %, symbols, thresholds,
and (for Testnet/Live) API keys.

## Database

`t3_engine/database/models.py` is the SQLAlchemy ORM (defaults to a local
SQLite file, zero setup). `t3_engine/database/schema.sql` has the same
schema as raw DDL plus the two `SELECT create_hypertable(...)` calls you
run once against Postgres/TimescaleDB for the two time-series-heavy tables
(`raw_trades`, `candles`) - SQLite has no hypertable equivalent, which is
why that step is manual rather than something the ORM layer papers over.

## Known limitations (read before treating anything here as "done")

1. **No live network access to Binance in this build sandbox** (this is a
   property of the sandbox this code was written in, not of Render or any
   normal hosting - Render has ordinary outbound internet access, so the
   dashboard's "Binance (live, public WS)" mode and `run_paper_trading.py`
   connect there without any changes). A direct test confirmed the
   outbound proxy returns a policy 403 on `CONNECT` to `fapi.binance.com`.
   This means:
   - The REST/WebSocket clients (`market_data/`) are correct against
     Binance's documented schema and unit-tested against mocked HTTP
     responses / fake sockets, but have never completed a real request in
     this session. Run `pytest tests/test_market_data.py -q` to see the
     offline coverage; run them for real wherever you have normal internet
     access.
   - The Testnet/Live execution engine
     (`execution/binance_futures_live.py`) implements and unit-tests the
     real HMAC-SHA256 request signing and `/fapi/v1/order` payload
     builder, but deliberately raises `NotImplementedError` on the actual
     network call rather than ship an unverified path that could place
     real orders. The module docstring gives the exact 3-step activation
     path once you have Testnet API keys and network access.
   - No real historical backtest (spec section 19's "minimum 1000 trades")
     has been run - it can't be, without real data. `backtest/` is fully
     implemented and its no-lookahead property is proven with tests; you
     need to run it yourself against real klines (`--source binance`).

2. **Corrective structure classification (zigzag/flat/triangle/combination)
   is a price-ratio heuristic**, not full recursive subwave counting.
   Automating full Elliott subwave counting to arbitrary depth is a
   research-level problem - even professional analysts disagree on counts
   looking at the same chart. `elliott_engine/rules_correction.py` explains
   the standard practitioner heuristic used (B-wave retracement depth,
   C-vs-A length ratio, leg-length monotonicity for triangles) and why a
   fully "correct" classifier isn't a solved problem to begin with.

3. **The probabilistic scenario engine's "top-3 alternative counts" are
   generated by varying the Wave-1 start anchor** among the 3 most recent
   same-type pivots - a concrete, testable stand-in for "where analysts
   disagree the move started." The spec's fuller vision of an
   unbounded, continuously-branching hypothesis tree across every nested
   degree simultaneously is described in the module docstring
   (`elliott_engine/scenario.py`) as a documented extension point, not
   implemented in full.

4. **The backtester evaluates entries on a single primary timeframe**
   (5m or 15m) and treats a confirmed wave transition as immediately
   reaching the TRIGGERED entry-timing stage, rather than replaying genuine
   1s-3m microstructure confirmation. This is because Binance does not
   serve historical sub-minute klines via REST at all (only recent
   aggTrades, which have a limited retention window) - see
   `market_data/rest_client.py` and `backtest/engine.py` docstrings. The
   **live pipeline** (`pipeline/live_loop.py`) does not have this
   limitation: it runs on genuinely causal real-time sub-minute data and
   drives the entry-timing state machine (`signal_engine/entry_timing.py`)
   properly through PREDICTION -> SETUP -> ARMED -> TRIGGERED -> CONFIRMED.

5. **Order book imbalance and some derivatives inputs are neutral-scored
   placeholders (0.5)** in the backtester/dashboard demo runs, since order
   book depth isn't in scope of REST historical klines and wasn't
   fabricated. `derivatives/tracker.py` and the order-book scoring hook in
   `signal_engine/scoring.py` are real and ready to be fed live data by
   `pipeline/live_loop.py`; wiring live depth-stream ingestion into that
   score is the next real piece of work, not a lie about what exists today.

None of the above are hidden - each one has a docstring at the point in
the code where it matters, plus a test that pins down the exact behavior
so a future contributor can replace the simplification without silently
changing what the surrounding tests assert.

## Staged rollout to real trading (spec section 24)

This build only exercises **PAPER** mode (simulated fills against real or
synthetic price data, zero exchange risk). To go further:

1. Get Binance Futures **Testnet** API keys, run `run_paper_trading.py`
   somewhere with real network access to confirm live data flows correctly
   end to end and signals look sane in the dashboard/decision log.
2. Wire the `# TODO` network call in
   `execution/binance_futures_live.py::submit_order` and point
   `base_url` at `https://testnet.binancefuture.com`; validate fills,
   partial fills, rejections, and `reduceOnly` behavior for real against
   Testnet.
3. Only after a genuine Testnet track record (the spec explicitly wants
   backtest -> walk-forward -> out-of-sample -> paper -> testnet -> live,
   in that order, section 19/24) should `base_url` move to
   `https://fapi.binance.com` and `TradingMode.LIVE` be used - and even
   then, start at a small size with the drawdown breakers
   (`risk_engine/risk_manager.py`) configured conservatively.
