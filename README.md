# elliott-wave-analyzer

Инструмент для анализа волн Эллиота с подключением к Bybit, расчётом Фибоначчи и визуализацией.

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
multi-timeframe Elliott Wave analysis and (paper-)trading engine, live
market data from Bybit USDT perpetuals (see "Mobile app + live Bybit"
below for why Binance was dropped). 260 automated tests, all passing,
cover every module described below.

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

**What is real code but not network-verified in this build:** the Bybit
REST and WebSocket clients that actually power the running app (correct
against the documented API schema, unit-tested against mocked responses/
fake sockets - confirmed working in production once deployed somewhere
with real network access), the now-dormant Binance clients kept for
reference, and the Testnet/Live execution scaffold (correct HMAC request
signing and order payload building, unit-tested offline). See "Known
limitations" below for exactly why, and the 3-step path to activate each
once you have network
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
  market_data/        Bybit REST + WebSocket clients (live); Binance clients kept dormant
  backtest/           event-driven, no-lookahead backtester + metrics + synthetic fixture
  pipeline/           live_loop.py - the section-20 event-driven real-time orchestrator
  dashboard/           FastAPI backend + static/index.html (lightweight-charts UI),
                      PWA manifest/service worker, live-pipeline start/stop/state endpoints
  ai_advisor/         optional BYO-key Gemini layer: second opinion, one-shot wave-labelling,
                      and the AI Analyst agent (playbook.py = the rulebook it is given,
                      analyst_tools.py = the tools it may use). Never a decision-maker.
  logger/             JSON-lines decision journal (SIGNAL_ACCEPTED/REJECTED + full context)

tests/                260 tests, one file per module above
run_backtest.py        CLI: run a backtest, print a metrics report
run_paper_trading.py   CLI: run the live pipeline against Bybit in PAPER mode
run_dashboard.py       CLI: serve the dashboard
```

Every module has a docstring explaining *why* it's structured the way it
is, not just what it does - read those before changing the hard-rule or
no-lookahead logic in particular.

## Mobile app + live Bybit + AI advisor (dashboard)

`dashboard/static/index.html` is a mobile-installable PWA, not just a
desktop web page:

- **DATA SOURCE IS BYBIT ONLY.** Binance was the original exchange, but in
  production it turned out unusable from this app's hosting: its REST API
  returned a sustained IP-level ban (HTTP 418 "I'm a teapot", the
  documented response for an auto-banned IP - likely inherited from other
  tenants on Render's shared outbound IP pool, since the ban appeared on
  the very first request a fresh deploy ever made), and separately its
  public WebSocket completed the connection handshake yet delivered
  *zero* trades indefinitely - confirmed via a running trade counter
  staying at 0 for many minutes on a real deploy, a silent failure, not a
  network error a client can even detect. Rather than keep working around
  an exchange that won't serve this app's traffic, **every code path here
  now talks to Bybit exclusively** (`market_data/bybit_rest_client.py` for
  history/symbols, `market_data/bybit_ws_client.py` for live) - confirmed
  working in production: a real live session received 2500+ real trades
  over 25 minutes with zero issues. The old Binance clients
  (`market_data/rest_client.py`, `ws_client.py`) still exist and are still
  unit-tested, dormant rather than deleted in case Binance access is ever
  restored, but nothing in the running app calls them.
- **"Add to Home Screen"** on iOS Safari or Android Chrome installs it as
  an app icon (manifest + service worker in `dashboard/static/`), no App
  Store/Google Play submission needed - that's the fast/simple path to a
  "mobile app" versus building and shipping a separate native app.
- **Live public WebSocket mode**: pick "Bybit (live, public WS)" as the
  source, enter a symbol in plain format (e.g. `BTCUSDT` - no slash, no
  quote-currency separator: `UNI/USDC` or `SOL/USDT` are rejected/
  normalized) and hit "Start live". This calls `POST /api/live/start`,
  which spins up the real `pipeline/live_loop.py` orchestrator against
  Bybit's **public** `publicTrade` WebSocket stream for that symbol - no
  API key required, since market data isn't account data. A 5m/15m candle
  only appears once a full interval has genuinely elapsed after Start -
  `trades_received` in `/api/live/state` climbs immediately as real trades
  arrive, well before the first candle closes, so you can tell "connected
  and receiving data" apart from a dead connection without waiting 5
  minutes to find out.
- **Symbol picker**: the symbol field is backed by a custom JS dropdown
  (not the native HTML `<datalist>` element - see below for why) fed by
  `GET /api/symbols` (cached in-process for an hour) - type any letter and
  a filtered suggestion list appears, no need to type the full ticker.
  When Bybit is reachable this lists every actively-tradeable linear
  (USDT-margined) perpetual symbol (`"source": "live"` in the response).
  When it isn't, `/api/symbols` **never returns an error**: it serves a
  hand-picked static list of ~60 long-established symbols instead
  (`t3_engine/market_data/fallback_symbols.py`, `"source": "fallback"`),
  so the picker is always populated with something real even during an
  outage, and swaps back to the live list transparently the next time a
  fetch succeeds.
- **Render free-tier cold starts**: a free Render web service spins down
  after ~15 minutes with no HTTP traffic and takes up to roughly a minute
  to wake back up on the next request - this looks exactly like an
  "infinite connecting" hang if nothing tells you what's happening. Every
  dashboard request now times out after 45s and shows a message explaining
  this instead of spinning forever; if you hit it, the fix is just to wait
  and retry, not to redeploy. A paid Render plan (or pinging the service
  periodically) avoids the sleep entirely.
- **PWA cache bug (fixed, in two layers)**: earlier versions of `sw.js`
  cached the app shell (`index.html`) cache-first and never re-fetched it
  - browsers only re-run a service worker's install/activate when the
    worker file's own bytes change, and since `sw.js` itself hadn't
    changed across several deploys that fixed real frontend bugs, phones
    with the PWA already installed kept serving the stale cached page
    indefinitely. `sw.js` now fetches the shell network-first (falling
    back to cache only when offline).
  Even after that fix, an **already-open tab** can still show stale UI:
  confirmed via Render's own request logs in production - a phone
  fetched `/` and `/sw.js` successfully seconds after a new deploy went
  live (fresh 200 responses reached it, proving the server and deploy
  were correct), yet its already-open tab kept displaying the old page,
  because nothing had told that specific tab's already-running JavaScript
  to restart - a new service worker taking over network requests in the
  background does not, by itself, make an open tab re-execute its script.
  Two things now close this gap: `index.html` listens for the browser's
  `controllerchange` event (fired exactly when a new service worker takes
  control) and reloads itself once when it fires, and `/` and `/sw.js` are
  now served with explicit `Cache-Control: no-store` headers so no
  intermediate cache (browser HTTP cache, a carrier/ISP compression
  proxy) can hand back stale bytes before the service-worker-update check
  even runs. If you still see old behavior after this, fully close the
  tab/app (not just background it) and reopen - that guarantees a clean
  script execution regardless of any in-memory state from before. A
  yellow `BUILD_VERSION` badge next to the title (also returned by
  `GET /api/health`'s `build` field) exists purely so a deploy reaching
  the server can be confirmed against what a user actually sees, with no
  ambiguity from any of the caching layers above.
- **Symbol autocomplete is a custom dropdown, not the native HTML
  `<datalist>` this used to rely on**: Mobile Safari accepts an input's
  `list` attribute without error but simply never renders the native
  suggestion popup at all (a long-standing WebKit gap) - so on exactly
  the platform this app targets, typing a letter produced no visible
  suggestions no matter how correct the underlying symbol list was.
  `index.html` now renders its own absolutely-positioned suggestion list
  in plain JS, filtered client-side from the same `/api/symbols` data,
  which works identically on every browser.
- **Chart marker overlap (BOS/CHoCH labels stacking into unreadable
  noise)**: structure events accumulate for an entire history/session with
  no cap from the backend; drawing all of them as text markers on a long
  series stacked dozens of labels on top of each other. This was mistaken
  for the underlying Elliott/market-structure detection being wrong - it
  was purely a chart-rendering limit (now capped to the most recent 30 via
  `MAX_STRUCTURE_MARKERS` in `index.html`), not a signal bug; wave labels
  were already correctly limited to the primary scenario only.
- **Wave numbers across the WHOLE history, not just the tail**: `rebuild()`
  in `elliott_engine/scenario.py` replaces its top-3 scenarios from scratch
  on every new pivot (that's correct for "what's the current best count"),
  which used to mean every earlier wave was silently discarded the moment
  a new one confirmed - the chart only ever showed numbered waves at the
  very end, with nothing behind them. `ScenarioEngine.wave_history` now
  keeps a permanent, append-only record of every wave any top-ranked
  scenario ever confirmed (keyed by label+start time so re-growing the
  same count on the next rebuild doesn't duplicate it), exposed as
  `wave_history` in `/api/run` and `/api/live/state` and drawn by the
  frontend alongside the full confirmed-pivot zigzag (`pivots`) - so the
  chart now shows how the count actually evolved across the loaded range,
  not just its current tail.
- **Deep history (up to 10,000 candles)**: Bybit's kline endpoint caps a
  single request at 1000 rows. `BybitFuturesREST.get_klines` now paginates
  automatically past that (walking backwards in time via the `end` param
  and reassembling pages in chronological order) whenever more is
  requested; `/api/run`'s `limit` now accepts up to 10000, and the
  dashboard has a "Candles" field (bybit source only) to ask for it.
- **Trading enabled on 1h/4h**: `TRADEABLE_TIMEFRAMES` (`common/types.py`)
  was originally `(M5, M15)` only, per spec section 21 - 1h/4h were
  context-only, since the backtester can't reconstruct genuine sub-minute
  entry confirmation on any timeframe (see "Known limitations" below,
  which now applies equally to 1h/4h). Widened to `(M5, M15, H1, H4)` on
  the project owner's explicit instruction to evaluate the same rule-based
  strategy on higher timeframes too; only 1s-3m stay confirmation-only.
  This is an honest scope change, not a fix: the single-timeframe
  entry-timing simplification still applies on every one of these degrees,
  so a 1h/4h backtest's win rate is no more (or less) reliable than a
  5m/15m one already was.
- **On backtest profitability**: this dashboard will not be tuned to hit a
  specific target return on a specific historical window. Picking
  parameters *after* seeing what makes one particular backtest profitable
  is curve-fitting/data-dredging - it reliably produces a strategy that
  looks great on that one window and has no real edge going forward, which
  is a worse outcome than an honest "it lost money on this sample." The
  risk engine's daily-drawdown breaker (`risk_engine/risk_manager.py`)
  halting further trades after a string of losses is that same discipline
  working as designed, not a bug to route around. Real profitability can
  only be established by running the (now wider) strategy over real
  history you fetch yourself and reporting the actual number, whatever it
  is - see `run_backtest.py --source bybit`.
- **Fibonacci overlay is now persistent, not just on an accepted trade**:
  before this, the ONLY Fibonacci-derived lines the chart ever drew were
  an accepted signal's TP/SL levels (`drawTradeLevels` in `index.html`) -
  gated by a confidence threshold and hard Elliott rules, accepted signals
  are rare, so for long stretches nothing Fibonacci-related appeared on
  screen even though `score_fibonacci()` (`elliott_engine/scenario.py`)
  was using it in every probability calculation the whole time. This read
  as "Fibonacci isn't being used" - understandable, since visually it
  wasn't shown, even though numerically it was. `fibonacci_levels_for_scenario`
  (`dashboard/server.py`) now projects retracement/extension levels for
  whichever wave the primary scenario expects NEXT, using the exact same
  functions `signal_engine/targets.py` uses for real TP/SL - drawn as a
  persistent dashed overlay regardless of whether any trade fires.
- **On wave-counting "not matching the book"**: a screenshot comparison
  against a manually-drawn TradingView count surfaced a real question,
  not just a display bug. This engine intentionally does NOT count a
  single continuous 1-2-3-4-5-A-B-C-1-2-3-4-5-... sequence the way a
  textbook walkthrough does; `elliott_engine/scenario.py`'s own module
  docstring explains why: real Elliott counting is inherently ambiguous
  even to expert human analysts ("did the new trend start at swing A or
  swing B?"), so `ScenarioEngine.rebuild()` generates candidate counts
  from the last 3 same-kind pivots as independent Wave-1-start hypotheses
  and keeps whichever scores highest - a genuine, if different, approach
  from "restart the count from scratch immediately after every completed
  ABC". This is a real architectural choice with a real tradeoff (it
  can produce counts that look locally right but don't chain into one
  tidy continuous narrative), not a bug curve-fitting could fix, and not
  something changed in this round - see "On backtest profitability" above
  for why a profit target is never the mechanism used to change how
  counting behaves.
- **Subwaves - "waves and subwaves should be accounted for"**: a motive
  wave (1/3/5) can now be subdivided into its own i-ii-iii-iv-v count.
  `elliott_engine/scenario.py::build_subwaves` runs a FRESH ZigZag pivot
  detector, at a finer deviation than the primary count, over just that
  wave's own candle range once it's fully confirmed - held to the
  identical hard Elliott rules as any primary count (`build_candidate_waves`
  was refactored to trigger those rules by position, not by digit-label
  identity, specifically so it works unchanged for either label scheme).
  `BacktestEngine.subwave_history` (mirroring `wave_history`) accumulates
  these across a run; exposed as `subwave_history` in `/api/run` and
  `/api/live/state`, drawn on the chart as smaller markers underneath the
  primary numbers. Only 1/3/5 subdivide (5 waves) - corrective waves
  subdivide into 3, which this engine doesn't label yet (WaveLabel has no
  micro a/b/c, only i-v - a real scope boundary, not an oversight).
- **A real stop-loss/take-profit fill bug, found from production
  feedback**: `PositionManager.on_price_update` was closing a position at
  whatever wick price (a bar's raw `.low`/`.high`) triggered the stop or
  TP, not at the position's own `stop_loss`/`take_profit` level. On a wide
  bar - routine on the 1h/4h timeframes just enabled for trading - that
  extreme can be dramatically further from entry than the risk engine
  ever intended, which is exactly what surfaced as real R multiples of
  -50 or worse on a live session (a 1% intended risk realizing a 50%+
  loss). Fixed to fill AT the stop/TP level regardless of how far the
  triggering wick ran past it - the standard, conservative backtesting
  assumption absent real tick-level gap data - symmetrically for both
  stops (previously overstating losses) and take-profits (previously
  overstating wins by the identical mechanism). This is the real
  explanation for the outsized losses in those screenshots, not
  insufficiently loose risk limits.
- **On "unlimited capital" and "unlimited stops"**: risk-per-trade is a
  FRACTION of equity (`risk_engine/risk_manager.py`), so position sizing,
  PnL and drawdown all scale proportionally regardless of account size -
  "unlimited capital" isn't a real lever to pull in this model, it just
  reduces to a bigger starting number, which `/api/run`'s new `equity`
  param (and a matching "Equity" field in the dashboard) exposes directly.
  Removing stop-losses ("unlimited stops") was declined: a trade with no
  defined risk boundary is the single practice every professional risk
  framework exists to prevent, not a refinement of one - especially now
  that the fill-price bug above is fixed and no longer needs a wider stop
  to "cover" it. The daily-drawdown breaker halting trading after a string
  of losses is the same risk engine doing its job, not something to widen
  or bypass either.
- **Server-side logging now actually reaches Render's log viewer**: a
  batch of connection-visibility logging (WS connect/disconnect, first-
  trade-received) was added in an earlier round and shipped, then
  confirmed completely absent from production logs despite the code
  definitely running - Python's root logger has no handler by default,
  and gunicorn/uvicorn only configure their own loggers, not arbitrary
  application ones. `server.py` now calls `logging.basicConfig(...)` at
  import time so every module's logger actually reaches stdout. This is
  exactly what surfaced the Bybit fallback working in production (see the
  first bullet above).
- **AI tab (Gemini, BYO key)**: paste your own Google AI Studio key (free
  at `aistudio.google.com/apikey` - stored only in your browser's
  `localStorage`, forwarded per-request and never written to disk
  server-side, see `ai_advisor/advisor.py`). Two separate features, with
  deliberately different amounts of trust:
  - **Second opinion** (`/api/ai/advice`): a skeptical plain-text critique
    of the current scenario/signal. Strictly advisory - it can never
    accept/reject a trade or move a stop; the rule-based engine already
    made that call before the model ever sees it.
  - **AI wave labelling** (`/api/ai/label`): the model proposes a count
    across the WHOLE loaded history - the one thing the deterministic
    engine deliberately won't do, since it only anchors on recent pivots.
    See "An external model proposes, the server disposes" below for why
    this is safe to expose at all.

  The **model name is editable in the AI tab** (saved next to the key in
  `localStorage`) rather than pinned in the code, because Google retires
  model names for NEW keys while existing keys keep working - the same
  build can therefore work for one person and 404 for another. This is not
  hypothetical: `gemini-2.5-flash` was the default until production
  returned `404 NOT_FOUND` with *"no longer available for new users...
  update your code to use models/gemini-3.6-flash"*. The default moved to
  what Google's own error named, and the API's error text is now surfaced
  verbatim plus a hint pointing at that field - so the next rename is a
  paste, not a redeploy. The correct model is a property of whose key it
  is, not of this deployment.

## The wave-count model: one chain, one current count

Two properties the engine now guarantees, both of them fixes to real bugs
rather than features:

**History is a single consistent chain, not an archive of guesses.** An
earlier version kept every wave label it had ever produced, keyed by
(label, start time). That sounds harmless and isn't: when the engine
re-anchors - which it does constantly, since `ScenarioEngine.rebuild()`
regenerates candidates from the most recent pivots - the new count
disagrees with the old one about the same stretch of candles, and BOTH
readings stayed on the chart forever. Old, incompatible 1/2/3/4/5s piled
up with every re-anchor. `confirmed_chain` replaces it with one invariant:
**at most one wave may cover any given moment**. A new reading supersedes
whatever it overlaps (truncate back to the divergence point, then extend),
so the chart shows one coherent history plus one current count, and
`tests/test_scenario_engine.py` asserts the no-overlap property directly.
Subwaves are pruned with their parent, so an `i-ii-iii` can never outlive
the wave it subdivides.

**A failed impulse dies; it is not relabelled.** `rules_diagonal.py` has
always said a diagonal must be PROVEN, not assumed - but the engine used
to "rescue" any impulse that broke the wave-4 overlap rule by silently
retagging it a diagonal, which inverts exactly that burden of proof. Now
impulse and diagonal are independent hypotheses generated over the same
pivots, each validated by its own rule set, competing on probability. A
broken impulse is simply invalid. Diagonals are still detected - as
diagonals, on their own merits. In the same spirit, A-B-C labels are now
only ever appended on top of a motive count that has already PASSED its
hard rules; before, that held by accident of control flow rather than by
rule.

## An external model proposes, the server disposes

AI labelling is useful precisely because a language model will read the
whole history at once. It is also completely untrusted: models routinely
return pivot indices that don't exist, waves that run backwards, or a
"1-2-3-4-5" whose wave 4 sits deep inside wave 1.

So the model never sends prices, labels-with-coordinates, or anything
drawable. It sends **indices into a pivot list the server computed
itself** (`/api/ai/label` recomputes pivots from the same series `/api/run`
uses - client-supplied pivots are ignored outright, or "the server
validated the indices" would be a claim about the caller's data rather
than about the chart). Everything that comes back goes through
`elliott_engine/external_count.py`, which checks, in order:

1. shape (a list of at most 8 legs);
2. canonical labels 1,2,3,4,5[,A,B,C] - no skipping, reordering or
   inventing, and no A-B-C without a full 1-5 in front of it;
3. indices that exist, run forward, and chain end-to-start (a count is one
   continuous structure, not disjoint fragments);
4. **causality** - every referenced pivot must have been confirmed at or
   before the caller's cutoff candle, so the external path cannot
   reintroduce the lookahead the rest of the engine is built to prevent;
5. alternation (each leg runs HIGH→LOW or LOW→HIGH, and leg 1 starts on
   the kind the trend requires);
6. the same hard Elliott rules as an internal count - by calling the same
   `build_candidate_waves()`, not a second copy of the rules that could
   drift from it.

A structurally impossible proposal is rejected outright; a well-formed but
rule-breaking one comes back with the rule it broke, which is a useful
answer ("you can connect those pivots, but that is not a legal impulse")
rather than a silent failure. Only a validated count is ever drawn, in its
own colour so it can't be mistaken for the engine's own conclusion.
`tests/test_external_count.py` is written from the attacker's side: each
test is a shape of nonsense a model realistically returns.

## The AI Analyst: a clean chart, an agent, and the same trust boundary

The AI labelling above is one shot: the model is handed a pre-computed
pivot list and returns a single count. If it misjudges the degree or
breaks the overlap rule, nobody finds out until the whole answer is
rejected and the chart stays empty.

The **AI Analyst** tab is the other approach. It gets its own bare chart -
candles and nothing else, no pivots, no BOS/CHoCH, no scenarios, no
Fibonacci - and works it the way an analyst does, in a loop, using tools
this server executes (`ai_advisor/analyst_tools.py`):

| Tool | What it does |
| --- | --- |
| `list_pivots(deviation_pct)` | The swing skeleton at a chosen degree. Large deviation = the higher-degree structure, small = subwaves. |
| `get_candles(start, end)` | Raw OHLC for a range, downsampled, for reading the shape inside a leg. |
| `measure_move(...)` | Exact price distance, % move and bar count between two pivots. |
| `fibonacci_levels(...)` | Retracements and extensions of a measured leg. |
| `check_count(...)` | **Runs the real rule engine** and returns the rule that broke. |
| `submit_count(...)` | The final answer - re-validated before it is accepted. |

It is given the rulebook explicitly (`ai_advisor/playbook.py`), written out
the way the literature separates it: the **hard rules** that make a count
wrong (wave 2 never passes the start of wave 1; wave 3 is never the
shortest; wave 4 never enters wave 1's territory - except in a diagonal,
which must then be *labelled* a diagonal), and the **guidelines** that only
change probability (alternation, equality, channelling, the Fibonacci
ratios, wave personality, degree consistency). It knows impulses,
extensions, truncated fifths, leading and ending diagonals, zigzags, flats
(regular, expanded, running), triangles and combinations, and it is told to
work top-down and to test each candidate count before committing to it.

Two things this does **not** change:

* **The trust boundary.** Every structure the agent submits is re-validated
  against the same hard rules before it leaves the server - including
  counts `check_count` already approved, because nothing forces a model to
  submit the thing it tested. Corrections are validated as corrections:
  `external_count.py` now carries the zigzag rules (B never passes the
  start of A; C always carries beyond the end of A), the flat rule (B
  retraces at least 61.8% of A, or it is a zigzag and not a flat - while
  expanded and running flats stay legal), and the triangle rule (five legs
  that contract throughout or expand throughout). An A-B-C cannot be
  submitted through the impulse path or vice versa.
* **What it may touch.** Six read-only functions over one candle series. No
  trades, no settings, no state, no network. The worst a confused model can
  do is waste its own steps.

Rejected structures are shown in the panel with the rule they broke, next
to a transcript of every tool call the agent made - the point being that
you can audit *how* it got there, not just what it concluded. It has its
own chart on purpose: a model shown an existing markup tends to agree with
it, and the two counts are only worth comparing if they were reached
independently.

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
4. Nothing here needs a Bybit API key (market data is public). If you
   later want the AI tab to work, you (or your users) just paste a Gemini
   key into the browser - no server-side config needed for that either,
   and the deterministic path runs fully without one.

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

# Run the automated test suite (260 tests)
pytest tests/ -q

# Run a backtest against the synthetic demo fixture (no network needed)
python run_backtest.py --cycles 3 --threshold 70

# Run a backtest against REAL Bybit history (needs network access to api.bybit.com)
python run_backtest.py --source bybit --symbol BTCUSDT --limit 1000

# Serve the dashboard at http://localhost:8000
python run_dashboard.py

# Run the live event-driven pipeline in PAPER mode against real Bybit
# market data (needs network access to stream.bybit.com)
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

1. **No live network access to any real exchange in this build sandbox**
   (this is a property of the sandbox this code was written in, not of
   Render or any normal hosting - Render has ordinary outbound internet
   access, so the dashboard's "Bybit (live, public WS)" mode and
   `run_paper_trading.py` connect there without any changes, and are
   confirmed working there in production). A direct test confirmed the
   outbound proxy returns a policy 403 on `CONNECT` to real exchange
   hosts. This means:
   - The Bybit REST/WebSocket clients (`market_data/bybit_*.py`) are
     correct against Bybit's documented schema and unit-tested against
     mocked HTTP responses/fake sockets, but have never completed a real
     request in this session. Run `pytest tests/test_market_data.py -q`
     to see the offline coverage; run them for real wherever you have
     normal internet access.
   - The Binance clients (`market_data/rest_client.py`, `ws_client.py`)
     are likewise real and unit-tested but now unused by the running app
     (see "Mobile app + live Bybit" above for why) - dormant, not deleted.
   - The Testnet/Live execution engine
     (`execution/binance_futures_live.py`) implements and unit-tests the
     real HMAC-SHA256 request signing and `/fapi/v1/order` payload
     builder, but deliberately raises `NotImplementedError` on the actual
     network call rather than ship an unverified path that could place
     real orders. The module docstring gives the exact 3-step activation
     path once you have Testnet API keys and network access. This part
     was left targeting Binance's Futures API since it's about real-money
     order execution (a separate, still-dormant concern from the market-
     data source switch above) - see "Staged rollout" below.
   - No real historical backtest (spec section 19's "minimum 1000 trades")
     has been run - it can't be, without real data. `backtest/` is fully
     implemented and its no-lookahead property is proven with tests; you
     need to run it yourself against real klines (`--source bybit`).

2. **Corrective structure classification (zigzag/flat/triangle/combination)
   is a price-ratio heuristic**, not full recursive subwave counting.
   Automating full Elliott subwave counting to arbitrary depth is a
   research-level problem - even professional analysts disagree on counts
   looking at the same chart. `elliott_engine/rules_correction.py` explains
   the standard practitioner heuristic used (B-wave retracement depth,
   C-vs-A length ratio, leg-length monotonicity for triangles) and why a
   fully "correct" classifier isn't a solved problem to begin with. It is
   now actually called from the main analysis pipeline for 3-leg zigzag/
   flat corrections (see "Core engine fixes" below for why it wasn't
   before); triangles and combinations still can't be built by this
   engine's current fixed-length wave-anchor scheme.

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

## Core engine fixes from an independent audit

A separate audit pass (never actually applied to this repo - verified via
`git log`/`git status` before trusting any of it) claimed several
fundamental bugs in the analysis engine. Each claim was independently
re-verified against the real code (not taken on faith) before anything was
changed; four turned out to be genuine bugs, now fixed and covered by
dedicated tests (`test_market_structure.py`, `test_scenario_engine.py`):

1. **Pivot self-confirmation** (`market_structure/pivots.py`): OHLC alone
   never reveals whether a candle's high or low happened first within the
   bar, so a single wide-range candle used to both SET a new extreme and
   immediately CONFIRM a pivot against that same just-set extreme (its own
   opposite wick trivially cleared the deviation threshold). Measured
   real-world impact: the project's own 3-cycle synthetic fixture (273
   candles) went from **267 pivots down to 24** with the exact same
   deviation setting - nearly one spurious pivot per candle before the
   fix. A candle that extends the tracked extreme now never also confirms
   a pivot on that same candle; confirmation requires a strictly later one.
2. **`rules_correction.py` and `rules_diagonal.py` were dead code** from
   the actual analysis pipeline's perspective - real, tested modules that
   `elliott_engine/scenario.py` never imported or called, so every A-B-C
   leg was labelled with no shape classification at all, and a wave-4/
   wave-1 overlap was always invalidated even when the leg was a genuine,
   correctly-shaped diagonal. Both are now wired into
   `build_candidate_waves`: a wave-4 overlap is rescued as
   `DIAGONAL_ENDING` only if it *also* passes the diagonal-specific
   contracting/expanding shape rules (an overlap alone is never enough,
   per that module's own docstring), and a completed A-B-C leg is
   classified (zigzag/flat/unknown) via `classify_correction`. Triangles
   (A-B-C-D-E) and combinations (W-X-Y) still can't be built here - this
   engine's fixed 8-label anchor scheme (`_IMPULSE_LABELS + _ABC_LABELS`)
   has no D/E or W-X-Y legs to classify yet, so those remain
   `UNKNOWN_CORRECTION` rather than being silently mislabelled. The
   result is exposed end-to-end: `Wave.structure_type` is now a real,
   populated field, serialized in `/api/run`/`/api/live/state` responses.
3. **BOS/CHoCH had no minimum-significance filter**
   (`market_structure/structure.py`): any new local high/low fired a
   structure event unconditionally, even a break a fraction of a point
   past the prior swing. `MarketStructureTracker` now takes a
   `min_break_pct` (wired through `BacktestConfig.structure_min_break_pct`,
   default 0.05%) that gates event *emission* only - swing-point
   bookkeeping still tracks the true latest extreme either way, so a
   later genuinely-significant break off a marginal swing still fires
   correctly.
4. **Scenario confidence was normalized across survivors, not scored
   absolutely** (`elliott_engine/scenario.py`): dividing by the sum of
   surviving scenarios' scores meant a single weak scenario that outlived
   the others got rescaled to exactly 100%, regardless of how good it
   actually was. Fixed to scale each scenario's own weighted score (already
   bounded in [0, 1], since its component weights sum to 1.0) straight to
   a 0-100 percent with no cross-scenario redistribution - a lone survivor
   now keeps its own honest score. **Important nuance the original audit
   claim got wrong**: this only affects the *displayed* scenario
   probability. The actual trade-acceptance gate
   (`signal_engine/scoring.py`'s `compute_confidence`/`evaluate_entry`,
   compared against `entry_confidence_threshold`) was already computing an
   independent, always-absolute score from raw component values - it was
   never fed the renormalized `scenario.probability` - so no live trading
   decision was ever affected by this bug, only the number shown in the
   dashboard's scenario panel.

One claim in the same audit - that Binance changed its WebSocket API to
require a separate route for aggTrade/markPrice/kline - could **not** be
verified at the time: this build environment has no outbound access to
Binance's real, current documentation. `market_data/ws_client.py` still
uses the historically-documented combined-stream format (`wss://
fstream.binance.com/stream?streams=...`), unchanged. This became moot
shortly after: production logs showed Binance's WS connecting successfully
but delivering zero trades for many minutes regardless, which is what
prompted dropping Binance entirely in favor of Bybit (see "Mobile app +
live Bybit" above) - so the specific route-change claim was never
actually acted on either way.

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
