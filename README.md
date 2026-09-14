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
below for why Binance was dropped). 420 automated tests, all passing,
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
  ai_advisor/         optional NVIDIA-API-Catalog layer: second opinion, one-shot wave-labelling,
                      and the AI Analyst agent (playbook.py = the rulebook it is given,
                      analyst_tools.py = the tools it may use). Never a decision-maker.
  logger/             JSON-lines decision journal (SIGNAL_ACCEPTED/REJECTED + full context)

tests/                420 tests, one file per module above
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
- **Live starts from history, not from nothing.** A live session used to
  begin with a genuinely empty chart: `LiveTradingEngine.history` was an
  empty list per timeframe, so pivots, the confirmed chain and every
  scenario had to be rediscovered from candles arriving *after* the
  connection - one full bar away on 5m, days away on 4h. `POST
  /api/live/start` now takes a `backfill` count (the frontend sends
  whatever the "Candles" field says, default 1000, max 5000) and
  `seed_live_history()` fetches that many candles per tracked timeframe
  from Bybit REST and replays them through `LiveTradingEngine.seed_history
  ()`. Those candles go through the **same** `_on_candle_closed` path a
  live one takes, so the no-lookahead guarantee is untouched - each is
  processed knowing only the ones before it - and `seed_history` is
  idempotent by `open_time`, so a reconnect that re-fetches overlapping
  history adds nothing twice. A backfill that can't be fetched degrades
  the session rather than refusing to start it, and the response says how
  many candles each timeframe actually got (`{"5m": 1000, "4h": 0}`), so
  an empty one is visible rather than assumed. `/api/live/state` reports
  `seeded_candles`, and the status line reads `1200 candles (1000 history
  + 200 live)` - which of those bars came from the past and which arrived
  on the socket is not something you should have to guess.
- **The model's markup stays on the live chart.** `/api/live/state`
  returns `ai_analysis`: the saved analyst count for that exact
  `(bybit, symbol, timeframe)` series, drawn on the live chart in the same
  colours the analyst tab uses (motive sky blue, corrective amber,
  projection dashed pink - one count shown in two places should not look
  like two different counts), with its 50-bar dashed projection carried
  forward past the newest candle. The stream then either walks into that
  projection or invalidates it in front of you. It travels with
  `candles_since` and `stale`, shown as "AI count age: 3 candle(s)
  behind": a count made twelve candles ago may still be right or may have
  been killed by the very next bar, and markup shown without its age reads
  as current when it is not. Both the analyst tab and the multi-timeframe
  view write to this same cache (`ai_advisor/analysis_store.py`), so a
  count is paid for once and then reused by whichever view asks for it;
  when nothing has ever been analysed for the series, the panel says so
  and names the tab to run, rather than leaving a blank.
- **Long analyses run as background jobs, not as one long HTTP request.**
  This is a fix for a *measured* failure, not a hypothetical one. A
  multi-timeframe run counts 5m, 15m, 1h and 4h and then reconciles them -
  four full analyst runs plus a synthesis. One such run was timed on the
  real deploy at **12 minutes 13 seconds** (`POST /api/ai/multi`,
  15:35:56 -> 15:48:09, in the service's own request log). The server
  produced a complete, correct answer and nobody ever saw it: the phone,
  the radio and the hosting proxy had all dropped that connection minutes
  earlier, so the page showed "Load failed" **after** the tokens had been
  spent. No timeout setting fixes that - a twelve-minute HTTP response is
  not something a mobile browser behind a CDN will hold open. So:
  - `POST /api/ai/multi/start` and `POST /api/ai/analyst/start` return a
    **job id in milliseconds**; the run continues on its own thread
    (`ai_advisor/jobs.py`). `GET /api/ai/job?job_id=...&since=N` returns
    its status, elapsed time and the progress lines the caller does not
    have yet (`since` keeps a long run from re-sending its whole
    transcript every poll). Each poll is a short request that can fail and
    be retried without costing the run, because the run is not on that
    connection.
  - **Progress is live.** Every analyst step and tool result is reported
    as it happens ("Step 4/10: fibonacci_levels -> 5 levels", "15m: reused
    the saved count"), shown under "Live progress". A spinner means
    "working" and "hung" equally; this does not.
  - **A second identical request joins the run in flight** rather than
    starting another one (`jobs.find_running`, keyed on kind + instrument
    + timeframe). Double-tapping Analyse used to buy the same four
    analyst runs twice.
  - **A reload, a locked phone or a closed tab costs nothing.** The job id
    is kept in `localStorage`, so the page reattaches on load and picks
    the progress up where it left off; if the run finishes while you are
    on another tab, that tab is marked. And the finished verdict is saved
    server-side (`GET /api/ai/multi/saved`), so even a session that ended
    entirely comes back to the answer it paid for, with the date it was
    computed.
  - The original synchronous `POST /api/ai/multi` and `POST
    /api/ai/analyst` are unchanged and still there for scripts and tests -
    they call the same `run_multi_work` / `run_analyst_work` functions the
    jobs do, so the two paths cannot drift apart.
- **A live session is the agent's chart, and only the agent's.** Live
  defaults to `ai_only` (`pipeline/live_loop.py`, `backtest/engine.py`):
  - **Nothing but the AI's markup is drawn.** `/api/live/state` sends
    empty `pivots`, `confirmed_chain`, `subwave_history`,
    `structure_events` and `fibonacci_levels` in this mode. They are still
    COMPUTED - the entry score needs the structure - but a second count
    drawn underneath the agent's is exactly the superimposed mess the
    analyst tab exists to avoid. `scenarios[0]` carries the AI's own count
    instead, so the panel under the chart reads the thing actually being
    traded.
  - **The paper trading is the agent's too.** `ai_advisor/ai_trading.py`
    rebuilds a tradeable `Scenario` from the agent's saved, server-
    validated structures and appends the wave now DEVELOPING (a saved
    count is a list of finished waves plus a projection naming what comes
    next; nothing is tradeable about a finished wave). That scenario then
    goes through the rules that already work: the wave-3/4/5/C entry
    plans, `evaluate_entry`'s scoring and confidence threshold, risk
    sizing, the stop and the targets. The trust boundary does not move -
    the model names waves, and **every price is still computed
    server-side**. Scoring deliberately reuses the engine's own formulas
    (same `score_fibonacci`, same validity, same invalidation level) so an
    AI count and an engine count sit on one scale.
  - **Nothing is retroactive.** A count takes effect for candles that
    close AFTER it arrives. Trading it back over the history it was
    derived from would be lookahead of the plainest kind: the agent saw
    that whole history before naming the waves. So a fresh live session
    with no saved count makes no trades at all, which is correct rather
    than a gap.
  - A structure no entry plan covers (a triangle, a flat) is drawn and
    never traded. Offering an entry there would mean inventing a rule this
    project does not have.
- **The live chart no longer ticks, flickers or drifts.** Every four-second
  poll used to call `setData()` with the whole series - a thousand candles
  re-sent and repainted - and then remove and re-add every overlay line.
  Now the poll sends only the tail (`candleSeries.update()` for the
  forming bar and any that closed since), and the overlays are rebuilt
  only when a signature of their content actually changes. Measured in a
  headless browser over five consecutive polls: **0 extra `setData` calls,
  0 series added, 0 removed, 6 `update()` calls** - one per bar.
- **The analyst tab accumulates every count that has been paid for.**
  `GET /api/ai/saved` lists every timeframe ever analysed for an
  instrument (newest work included, shortest timeframe first), and the tab
  shows them under "Saved counts" with when each was computed, how much of
  its chart it labelled and what it expects next. Click one to draw it -
  nothing is recomputed. A multi-timeframe pass computes four counts and
  saves all four; before this they vanished from that tab the moment it
  was reopened. A new run now **adds** to the list rather than replacing
  it.
- **Every stop and take-profit leg is recorded, and the agent is told.**
  A take-profit is a PARTIAL close: a position with two of its four legs
  filled stays open, and the panel could only say "open positions: 1,
  closed trades: 0" while two real fills sat inside it. `on_price_update`
  had been returning a reason for every fill and the loop discarded it.
  Now:
  - `backtest/engine.py` surfaces each fill through an `on_fill` hook, and
    the live loop writes it to `ai_advisor/trade_journal.py` - entry, each
    take-profit leg, the stop, the close - tagged with the AI count that
    planned it. The dashboard shows a Fills table plus realized P&L on
    still-open trades.
  - `stops_hit` counts stop EXITS, not losses: the runner leg exits on a
    trailing structural stop and is often the most profitable of the
    three. Wins and losses are decided by the sign of the money.
  - On its next run the analyst's opening brief carries one factual line -
    how many trades previous counts of this chart opened, how many legs
    filled, how many stops hit, and the realized total. It is phrased as
    history and carries **no instruction**: "your last count lost, so try
    something else" is how a model is talked into fitting its next answer
    to the last result instead of to the chart.
- **What a run costs, measured rather than guessed.** The provider's usage
  trailer was being dropped on the floor by the SSE reader;
  `stream_options: {include_usage: true}` now asks for it and
  `ai_advisor/usage.py` sums tokens across the dozen calls one run makes.
  Prices are **read from the provider's own `/v1/models` catalogue**, never
  hardcoded (a price typed into this repo would be wrong the first time
  the provider changed it, and wrong silently). Cached input tokens are
  billed at the published cache-read rate, which matters a lot here: an
  agent loop resends its conversation every step, which is exactly the
  shape that hits a prompt cache. The analyst panel shows the run's tokens
  and dollar cost, and what the same run would come to repeated hourly and
  every five minutes - the run rate is **named, not assumed**, because
  continuous monitoring is nothing but a run rate.
- **Saved work survives a deploy.** The default `T3_DATABASE_URL` is a
  SQLite file on the container filesystem, and that filesystem is replaced
  on every deploy: every labelled chart and every journalled fill was
  erased by the next push. There are now two ways out, and `/api/health`
  reports which one is in force (`storage_backend`):
  - **Supabase over REST** (`T3_SUPABASE_URL` + `T3_SUPABASE_SECRET_KEY`,
    `database/supabase_rest.py`). This is the path that actually works on
    a free Supabase project: a Postgres URL needs the database password,
    which Supabase shows exactly once at creation and never again, and a
    new project's DIRECT host is IPv6-only, which Render cannot reach at
    all. The service key is retrievable from the dashboard at any time and
    PostgREST answers over ordinary IPv4 HTTPS. The layer is deliberately
    thin - insert, filtered select with order and limit, filtered delete -
    because that is all the analysis cache and the trade journal need.
  - **Any Postgres URL** in `T3_DATABASE_URL`. `database/session.py` gains
    `is_durable()` and `normalize_database_url()` (providers print
    `postgres://`, which SQLAlchemy 2 refuses outright, so their string is
    rewritten rather than left as a trap) and psycopg is in requirements.

  An explicit `database_url` argument always beats both, which is what
  keeps tests on their own SQLite file even on a deployed box. And every
  millisecond-epoch column is `BigInteger` now: on SQLite the old `Integer`
  was harmless, on Postgres `INTEGER` overflows at 2.1e9 while a
  millisecond epoch is ~1.77e12.
- **In live, the whole chart is derived from the agent's count** - not just
  the wave labels. The Fibonacci grid is projected for the wave the agent
  says is forming now, and the subwave detail under waves 1/3/5 is
  subdivided from the agent's own waves (`ai_trading.subwaves_for`, using
  the engine's `build_subwaves` - a finer ZigZag graded by the same hard
  rules, so the subdivision is still the server's arithmetic, not the
  model's claim). A finer degree drawn from a different reading than the
  labels above it is not extra detail, it is a contradiction on the same
  candles. Two conventions had to be reconciled for the grid:
  `fibonacci_levels_for_scenario` expects completed waves plus the
  projected one named separately, while a tradeable scenario carries the
  developing wave inside `waves` - handing it the trading shape asked for
  the grid of the wave AFTER the one forming (wave A after a developing 5,
  which has no formula here) and returned nothing. `grid_scenario()`
  re-shapes it rather than papering over the empty result.
- **The synthetic source is a real chart per timeframe now.** It used to
  return the same 5m series whatever timeframe was asked for, so a
  multi-timeframe run analysed one chart four times and then "reconciled"
  it with itself. The model caught it before anyone else did - it wrote
  *"the supplied data repeats 5m"* into its own verdict and refused to
  call a trend, and the saved run shows `reused: ["5m","5m","5m"]`.
  `generate_synthetic_series_for(timeframe, cycles)` now aggregates 5m bars
  up to the requested timeframe exactly as an exchange builds a 4h bar out
  of its 5m ones (first open, highest high, lowest low, last close, summed
  volume; a trailing partial group is dropped rather than emitted short).
  A 4h series needs ~96 cycles of base data, and cycles compound at about
  +53% each, which took the generator to 3e10 and turned the "chart" into a
  vertical line - so the base series now alternates up and down cycles,
  with the down leg scaled so each pair lands back where it started. The
  mirrored cycle is still a hard-rule-valid impulse: a reflection negates
  every price difference and scales them all equally, leaving the ratios
  (and therefore R1-R6) intact.
- **Model settings.** Temperature for analysis is **0.3** (project owner's
  instruction). The case for 0 is recorded in `advisor.py` because the
  trade-off is real - at 0 two runs over the same candles agree - but the
  agent loop *explores* over a dozen-plus steps, and a little sampling lets
  it try a different degree or anchor instead of walking the same path to
  the same local answer. Every structure is still re-validated
  server-side, so a worse count costs a step, not a bad label; what it
  costs is reproducibility, and `DEFAULT_SEED` is what pins that back down.
  Reasoning effort defaults to **max**: a run is a background job you can
  close the tab on, so slowness stopped being a reason to hold back.
  "Not sent" remains an explicit choice for a model that answers `400` to a
  parameter it has never heard of.
- **Step budget, set from measurement.** Nine saved analyses of the same
  charts, read back out of the analysis cache:

  | steps | coverage | validated structures |
  |---|---|---|
  | 10 | 40.8 / 89.3 / 93.9 / 98.4 / 98.9 % (mean **84.3**) | 2 / 4 / 4 / 5 / 7 (mean **4.4**) |
  | 15-16 | 95.3 / 97.0 / 98.1 / 99.5 % (mean **97.5**) | 9 / 10 / 10 / 12 (mean **10.3**) |

  Raising 10 -> 16 roughly **doubled** the validated structures and removed
  the collapse case (one 10-step run labelled 40.8% of its chart; the worst
  16-step run managed 95.3%). And every 15/16-step run spent its whole
  budget - the model never stopped on its own, so the ceiling was still the
  binding constraint, not the point where it had nothing left to add. The
  default is **24** with a ceiling of 40, and the run budget went from 600s
  to 1800s so the wall clock does not quietly become the new cap.
  `steps_used` comes back with every result, so this can come back down on
  the same kind of evidence it went up on.
- **Volume and momentum tools**, added because the model kept referring to
  both and had neither. `volume_profile(start, end)` returns a stretch's
  total and average volume, its taker-buy share (above 0.5 means buyers
  were the aggressors) and its busiest bar; `momentum(start, end)` returns
  MACD, ADX, the DIs and the 9/18 EMAs **as at** `end_index` - fed the
  history up to that bar and never beyond - plus the MACD high and low
  reached inside the range, which is what a divergence is actually read
  from. Their Elliott use is specific and now in the playbook (G10/G11):
  wave 3 normally carries the heaviest volume and wave 5 makes its higher
  high on less, and a fifth wave topping on a lower MACD peak than the
  third is the classic ending divergence. Both are evidence that separates
  two counts which each pass the rules - they never override the rules.
- **A "Claude" tab: a second reading, held beside the first.** Counts made
  outside the analyst loop are stored under their own source
  (`CLAUDE_SOURCE`) rather than sharing the analyst's key, because two
  readings of one instrument are only useful if you can hold them side by
  side - a shared key means the newer one silently replaces the older.
  `GET /api/claude/timeframes` lists the timeframes that have a count
  (shortest first, and a timeframe with nothing stored is absent rather
  than present-and-empty), and `GET /api/claude/chart` returns the bars
  and the count together. The bars come from the stored series
  (`candle_store`) aggregated up to the requested timeframe, so the count
  is drawn on exactly the bars it was made on rather than on a freshly
  fetched window that has since moved. A timeframe FINER than what is
  stored is refused with the reason - 5m does not divide out of 15m, and
  returning something plausible would be inventing bars that never traded.
- **Aggregation aligns to absolute time buckets.** An exchange's 4h bar
  starts at 00:00, 04:00, 08:00 UTC; it does not start wherever the data
  happens to begin. Chunking positionally gives bars of the right LENGTH
  at the wrong OFFSET, and a wave labelled on one alignment does not line
  up with a chart drawn on the other.
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
- **AI tab (OpenRouter)**: runs against `openrouter.ai/api/v1`,
  `~openai/gpt-astra-latest` by default. The key
  can come from the browser (stored only in `localStorage`, forwarded
  per-request, never written to disk server-side) or from `T3_AI_API_KEY`
  on the server, with a request's own key always winning. **A server-side
  key makes the app an open proxy** to whoever owns it, because this
  dashboard has no login - `/api/ai/config` therefore reports only
  *whether* one exists, never its value. The wire format is
  OpenAI-compatible chat completions. Three separate features, with
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
  - **AI Analyst** (`/api/ai/analyst`): an agent that labels a clean chart
    from scratch using server-executed tools. See its own section below.

  Both the **model id and the API base URL are editable in the AI tab**
  (saved next to the key in `localStorage`) rather than pinned in the code.
  Catalogues change - which models exist and which support tool calling -
  and the build sandbox cannot reach `integrate.api.nvidia.com` to confirm
  anything, so the defaults come from NVIDIA's own API-catalog snippet
  rather than from a verified call. If every request 404s regardless of
  model, the endpoint is wrong and the fix is a paste, not a redeploy.
  `T3_AI_API_BASE` sets it server-side; a pasted full endpoint is not
  doubled up.

  The API's own error text is surfaced verbatim plus a hint, and the status
  code is most of the diagnosis: `404` = the model id or the endpoint,
  `401/403` = the key, `400` = a parameter this model rejects (clear
  reasoning effort or seed). A `200` carrying an error body - including one
  that arrives mid-stream, after the headers already said OK - is treated
  as an error too, since printing it as an empty second opinion would read
  as "the model had no concerns".

  **A busy provider is retried, not reported.** "Service temporarily
  overloaded" arrived from NVIDIA through OpenRouter *inside a 200*, and
  failing a whole run over a queue that clears in seconds is what sends
  someone swapping model ids by hand. Transient upstream messages
  (overloaded, capacity, unavailable, try again) and any `5xx` now go
  through the same retry-with-backoff as a rate limit. A non-transient
  error is not retried - three attempts at a bad key only make the wrong
  answer slower. The classification also applies to mid-stream error
  *frames*, since chat calls stream and a check that only covered the
  status code would never fire where it is needed.

  **The Model field takes a LIST**, comma-separated, sent to OpenRouter as
  its native `models` array: the router walks it in order and uses the
  first that answers, inside the same request. A free model behind a shared
  GPU pool is saturated a good fraction of the time, and that is what a
  router is for - not a person watching a panel and pasting a new id. Every
  entry is checked for being a chat model up front, because a wrong-kind
  model sitting third would otherwise surface only once the first two were
  busy, which is the worst possible moment to find out. **Fill Model field
  with working models** populates the list from the catalogue's own
  tool-calling metadata, free ones first.

  **`429` is retried, not reported.** Rate limiting is the dominant failure
  on a free tier and it is a *wait*, not a defect: the run was going fine
  and the quota window closed. Up to three retries with growing backoff
  (4s, 12s, 30s), and the server's own `Retry-After` wins over that when it
  sends one - guessing shorter than the quota window burns another attempt
  and on some services extends the ban. Only after those does it surface,
  with advice that fits the cause (wait, lower the step budget, lower the
  reasoning effort, or change model). This matters most for the analyst,
  which makes one call per step: a 14-step run is 14 chances to be
  throttled, and `reasoning_effort: max` makes each step cost more, so it
  hits the limit soonest.

  **Timeouts are split by phase**, because one number for everything gives
  the wrong diagnosis. Connecting is either fast or broken (15s); *reading*
  is the slow part - a free router model queues behind other traffic and a
  reasoning model thinks before its first token, so a read can legitimately
  take minutes. A read timeout therefore means the connection worked and
  the model was still busy, which has a completely different fix from a
  wrong URL, and the error now says so instead of "could not reach the
  API". The read budget is a field in the AI tab (default 180s).

  **Test key, URL and model** (`/api/ai/ping`) sends one request and
  returns the moment the first token arrives, without waiting for the rest.
  That distinction is the whole point: this check was originally
  non-streaming, which meant it waited out a reasoning model's entire
  thinking phase - so the one call meant to diagnose slowness was the one
  most likely to time out, which is precisely what happened with
  `deepseek-v4-pro` (60s timeout on a one-token request whose connection
  was fine). It also sends no `max_tokens` cap, since on most APIs a
  reasoning model's thinking counts against it and a small cap can end the
  generation before any visible token exists - indistinguishable from a
  hang.

  **Diagnose** (`/api/ai/diagnose`) is the one that ends the guessing, and
  it runs two checks in the order that narrows the problem:

  1. `GET {base}/models` - a catalogue listing. It needs the key and hits
     the same base URL, and it runs **no inference at all**. If it answers
     fast, the key, the endpoint and the network are correct *by
     construction*, and everything slow afterwards belongs to the model or
     the queue in front of it. If it fails, the problem is the key, the
     endpoint or the path, and no amount of waiting on a chat call would
     have shown that.
  2. A raw look at the chat endpoint: status line, response headers
     (allowlisted - a diagnostic that echoes arbitrary upstream headers is
     one change away from leaking something), whether the body is really
     server-sent events, the first bytes as they arrived, and the timing of
     each phase.

  Neither half raises on a timeout: a timeout *is* the observation and the
  partial result is the evidence. Every other error message in this module
  is a sentence written from an assumption about the cause, and "the model
  did not answer in 180s" is the same sentence whether the cause is a slow
  model, a request the gateway queued with `202`, a gateway that buffered
  the whole answer despite `stream: true`, or a typo in a URL. The verdict
  is reported separately from the observations, so a wrong reading of the
  evidence never hides the evidence.

  The catalogue listing also answers the one question that decides whether
  the AI Analyst can work at all: **does the chosen model support tool
  calling?** OpenRouter publishes `supported_parameters` per model, so this
  is read from the catalogue rather than guessed from the model's name, and
  absent metadata is reported as *unknown* rather than as "no" - absence of
  evidence is not evidence of absence, and reporting "no tools" there would
  send you chasing a problem that may not exist. When the answer is no, the
  panel says which of the catalogue's models do support it, free ones
  first: the second opinion and whole-history labelling still work without
  tools; only the agent loop needs them.

  Models of the **wrong kind** are refused before a request is spent on
  them. An embedding, rerank, moderation, speech or image model is a real,
  working model that simply has no text output and no tool calling - it
  returns vectors or scores from a different endpoint - so pointing the
  analyst at one fails in a way that reads as a broken app rather than a
  wrong choice. Matched on the id, and deliberately narrow so it cannot
  reject a working chat model over a substring.

  Third, a **configuration probe**: the same trivial prompt sent under
  several settings, each differing from the previous one by exactly one
  thing, so the first that answers names the cause rather than hinting at
  it. This exists because the first two checks answered the wrong half of
  the question. Against `deepseek-v4-pro` they proved the catalogue listing
  returns in **0.08s** while the chat endpoint sends **no response headers
  at all for 45s** - conclusive that the key, URL and network are fine, and
  that the gateway buffers the entire response before sending any of it, so
  the wait is the full generation. What they could not say is *which*
  request setting makes that generation long. The probe can: if "thinking
  off" answers and "thinking on" does not, the model's thinking mode is the
  whole problem and the fix is a toggle.

  That toggle is **Model thinking** in the AI tab, sent as OpenRouter's own
  `reasoning: {enabled, effort}` - the field a router normalises reasoning
  to across vendors. (It was previously `chat_template_kwargs: {thinking}`,
  which is the right field for models served directly by NVIDIA and the
  wrong one here.) It is **on by default** now that the model is paid: it
  was off while the free models sat behind a shared GPU pool, where
  thinking meant minutes before the first byte, but on a paid model
  thinking is the reason to use it and an Elliott count is exactly the kind
  of work it helps. Tri-state, since "do not send the field" is a distinct
  and necessary choice - a model that has never heard of it answers `400`
  rather than ignoring it.

  **The model's reasoning is carried across steps.** OpenRouter returns
  `reasoning_details` on the assistant message, and a reasoning model
  resumes from those on the next turn. The agent loop is many turns long,
  so dropping them would make every step start its thinking over - worse
  answers and a larger bill. They are accumulated from the stream by index
  and passed back **unmodified**: some are signed or encrypted blobs, and a
  re-encoded blob is a broken one. The tool-history trimming deliberately
  touches only tool result *bodies*, never an assistant turn, for the same
  reason.

  Two behaviours it catches are also *handled* rather than only reported: a
  `202` says outright that the request was queued rather than answered, and
  a non-SSE body that is nonetheless a valid completion is parsed and used
  instead of being waited out and then reported as an empty stream.

  What the plain check returns is a diagnosis rather than a yes/no: the **time to the
  first token**, because the analyst pays that once per step, so it is the
  number that decides whether a multi-step run is feasible at all. Under 5s
  means any step budget works; 40s means a 10-step run cannot finish and
  the advice says to use a faster model or cut the budget to 4-6. Thinking
  counts as a sign of life - refusing to count it would report a working
  setup as broken. And a stream that closes without a single token says so
  explicitly: key, URL and model are fine, that model produced nothing.

  **Chat calls stream** (SSE), and the chunks are reassembled into the
  ordinary response shape before anything downstream sees them. This is not
  cosmetic: a heavy reasoning model can think for minutes before its first
  visible token, and a non-streaming request spends that time on a silent
  socket - which production hit as a read timeout that discarded a whole
  run. Streaming keeps resetting the read clock, so the wait is bounded by
  the whole-run budget instead of by one silent gap. The tool-call
  reassembler keys fragments on `index`, because arguments arrive a few
  characters at a time and two parallel calls interleave - splicing them
  produces JSON that parses fine and describes a wave nobody proposed.

  **Generation settings, per job rather than one number everywhere:**

  | | temperature | max output | notes |
  | --- | --- | --- | --- |
  | Wave count / Analyst | **0.0** | 16384 | A count is not a creative task |
  | Second opinion | 0.3 | 2048 | Prose a human reads |
  | Connection check | 0 | uncapped | Returns on the first token |

  Temperature 0 for anything analytical is the whole point: the chart
  either does or does not contain a legal impulse, so sampling above 0 asks
  the model to sometimes prefer a count it thinks is worse. That is a
  defect in an analysis tool, not variety - and it makes runs reproducible
  without needing a seed. Commentary stays mildly warm only because a
  second opinion phrased identically every time stops being read.

  **Seed is no longer sent by default.** At temperature 0 decoding is
  already deterministic, so it added nothing while remaining one more
  parameter a provider can reject with a 400 - on an endpoint the user is
  free to point anywhere. It stays available for deliberate sampling.

  **The conversation no longer grows without bound.** The whole history is
  resent on every step, so a `list_pivots` result used to sit in it and be
  re-read by the model on every subsequent call: by step 8 a run was
  carrying tens of kilobytes of pivot lists it had already used, paying for
  them in latency on every call and bringing a free tier's rate limit
  forward. Only the newest three tool results now stay in full; older ones
  collapse to their one-line summary plus "call the tool again if you need
  the detail". Over a 10-step run that halves what is resent (352 KB ->
  177 KB measured). Only result *bodies* are trimmed - dropping an
  assistant turn or a `tool_call_id` would break the call/response pairing
  the wire format requires.

  Tool payloads shrank too: prices are rounded to seven significant figures
  rather than eight decimal places (eight decimals on a 77000-point
  instrument is thirteen characters of noise per pivot, re-read on every
  later step), and the candle and pivot caps came down to 200 and 250.

  **Reasoning effort** and **seed** are sent only when set. NVIDIA's own
  snippet for this model uses `reasoning_effort: max` - the best answer and
  by far the slowest, and the analyst makes one call per step, so the cost
  multiplies. An unsupported parameter is a `400` rather than a graceful
  ignore, and the endpoint is user-editable, so an unset field is an
  *absent* field. The seed defaults to 0 so the same chart tends to produce
  the same count.

  Provider history, since this keeps moving: OpenAI → Google Gemini →
  OrcaRouter → NVIDIA API Catalog → OpenRouter. Nothing downstream of `ai_advisor/`
  cares which model answered, which is why each swap has been a rewrite of
  one module plus its tests rather than of the engine.

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

## kumo-relational: trade quality, not a second analyst

`kumo-relational` is a different *kind* of model from everything else in
`ai_advisor/`. It takes a relational schema plus rows and returns a
prediction and a probability per row. It has no text output and no tool
calling, so it **cannot label waves, hold a conversation, or replace the
chat model** behind the AI Analyst.

It fits exactly one job here, and fits it well. The engine already produces
the table it wants: every signal carries its eight score components
(elliott, price_action, fibonacci, volume, momentum, derivatives,
orderbook, higher_tf) plus confidence and risk/reward, and every closed
trade carries its outcome. So "given how this setup scored, how often did
setups like it end green?" is a binary classification over the engine's own
past.

**Strictly advisory.** It never gates a trade, never moves a stop and never
edits a count - the hard Elliott rules and the risk engine decide, exactly
as with the second opinion. A model fitted to a few dozen of the engine's
own past trades is a hint, not an edge, and treating it as one would be the
same mistake as curve-fitting the parameters.

Two refusals matter more than the number:

* **Too few closed trades** (under 12) returns the reason rather than a
  probability. A number computed from four trades would be believed, and
  should not be.
* **A history where everything won or everything lost** is refused too: a
  table with one outcome can only repeat that outcome back.

The result always carries the size and balance of the history behind it,
and the dashboard prints each probability against that base rate - a 60%
that beats a 44% baseline says something very different from a 60% that
does not.

**No lookahead**, on the same terms as the rest of the engine: context rows
are filtered here to trades that had already *closed*, and open trades are
excluded entirely. The model is given `anchor_time` and could enforce that
itself, but a guarantee that depends on someone else honouring it is not a
guarantee - the same reason external wave counts are re-validated on this
server.

Note the host: this lives on `ai.api.nvidia.com`, **not** the
`integrate.api.nvidia.com` the chat models use, though it takes the same
key. Crossing the two produces a 404 that reads like a broken model id, so
the error message names which URL it means.

## Multi-timeframe: four charts, one opinion, nothing counted twice

A count from a single timeframe answers a question nobody asked. A
five-wave advance on 5m sitting inside a 4h correction is a bounce, and
only looking at both says so. The **Multi-TF** tab counts 5m, 15m, 1h and
4h, then makes **one** synthesis call that has to reconcile them - naming
where they agree, where they conflict, what it expects, and the price that
would end the read. Conflicts are reported rather than smoothed away: a
tidy verdict that hid a disagreeing timeframe is the most expensive kind of
tidy.

**Analyses are cached on DATA, not on a clock.** A full analyst run is a
dozen model calls over a whole history; four of them per request would buy
the same conclusions again every time. A saved analysis stays valid until a
candle arrives that it never saw - `last_candle_time` records the newest
one it did - so a 4h chart, which produces one new candle every four hours,
is usually read straight from the cache. The check is exact rather than
tolerant: one new 4h candle can end a wave, and "close enough" is how a
stale count survives the bar that invalidated it. An empty result is never
cached, since that would suppress the retry that might have worked. The
response names which timeframes were reused and which were recomputed, so
the saving is visible rather than claimed, and **Forget cache** is the
escape hatch.

**The synthesis sees conclusions, not histories.** Each timeframe's
structures, projection and coverage go into that one call - a few hundred
tokens - never the candles or pivots again. The per-timeframe work already
happened; asking the model to re-derive it would be paying twice inside one
request.

**The percentages are measured, not asked for.** Every target carries the
share of *this chart's own* completed swings that carried at least that far
relative to the swing before them, with the sample size alongside. Asking a
model for a percentage returns a confident number with nothing behind it,
and a percentage reads as measurement even when it is invention. Under
twelve swings no number is printed at all and the response says why - a
figure from nine swings is believed exactly as much as one from nine
hundred, which is the whole problem. The number is a base rate, not a
forecast: it knows nothing about where the count says price is now, which
is precisely why it is worth showing beside a forecast.

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
| `fibonacci_levels(...)` | Retracements, extensions, and - with `project_from_pivot_index` - a leg's length **projected from a third pivot**, which is how every Elliott target is actually built. Custom ratios allowed. |
| `fibonacci_confluence(...)` | Prices where levels from 2-6 different legs coincide. One leg's 61.8% is a line; three legs agreeing within half a percent is a zone. |
| `swing_statistics(...)` | What *this* chart does: median retracement of the previous leg and its quartiles, bars per swing, up legs vs down legs. |
| `check_count(...)` | **Runs the real rule engine** and returns the rule that broke. |
| `submit_count(...)` | The final answer - re-validated before it is accepted. |

The loop needs a model that supports **tool calling**. Not every catalogue
model does, and one that doesn't will answer in prose instead - which
comes back as `finished: false` with the model's own last message attached,
rather than as an empty result presented as a finished analysis.

**Submissions accumulate, and the chart gets finished.** `submit_count` can
be called many times; each result reports what percentage of the history is
now labelled and which stretches are still bare, and the run continues
until the model sets `complete=true` or the gaps are gone. Before this, the
first submission ended the run - which is how a live run came back with the
oldest third labelled and the recent two thirds untouched, the model having
simply stopped with nothing asking it to go on. The recent part is the part
anyone trades, so the playbook now says to reach the last candle.

**Forecasting leans on the chart's own habits.** Every Fibonacci ratio in
the playbook is a tendency, and a tendency is only worth using if it holds
on the instrument in front of you - so `swing_statistics` measures what
this history actually did, and the playbook says to prefer those numbers to
the textbook ones when they disagree and to say which were used.
Alternation is treated as a *forecast* rather than an observation: if this
chart's corrections have alternated sharp/sideways, the next one is more
likely to be the opposite of the last, and a shallow wave 2 argues for a
deeper wave 4 - but only after checking that this chart alternates at all.

**The count says where price goes next.** A count that stops at the last
confirmed pivot answers "what happened"; the reason to count waves at all
is what the count implies comes next, and that is arithmetic over waves
already on the chart. So the model names which structure is still unfolding
and which wave it expects (2, 3, 4, 5, B or C), and **the server computes
the targets** with the same Fibonacci code the deterministic engine uses -
wave 5 off wave 1's length from the end of wave 4, wave C off wave A's from
the end of B, and so on. The model never supplies a price. The dashboard
draws it **50 candles past the last one**, dashed and in its own colour,
because it is the one thing on that chart that has not happened yet - a
target with no time axis is a horizontal line that never expires, and a
path that stops at the last candle is invisible. The path runs to the
*middle* ratio rather than an extreme, since a path drawn to a tail of the
distribution gets read as a forecast of the tail; the other targets fan out
as thinner lines and as labelled levels, with the primary one starred. The
model may say how many candles it expects the wave to take (from
`swing_statistics`, not a guess) - that changes how fast the path reaches
the target, never the size of the window, so two runs stay visually
comparable. A projection that needs a wave the count does not have comes
back absent rather than invented.

A run that **stalls part way keeps the work it already did**. The agent
makes up to `max_steps` sequential calls, so a slow model can exhaust the
per-request timeout mid-run; when that happens the tool transcript up to
that point comes back with the error attached, because "it listed the
swings, measured wave 3, then the model stopped answering" is information,
and throwing it away leaves the user with a blank panel after a three-
minute wait. Only a failure on the *first* call raises - there is nothing
to show then, and an empty result would read as a finished analysis that
found no structure. A whole-run wall clock (480s) bounds the loop
independently of the step count, so a proxy in front of the app never gives
up before the app does.

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

The panel shows the **conversation itself**: what the model said, what it
was thinking (where the model streams reasoning separately, which this one
does), what it called, and what came back - in order, numbered by step. The
question "why did it stop at step 2" is unanswerable from a list of tool
names, and guessing at it was costing real debugging time.

A step budget that merely cuts the model off produces nothing, so on its
**last allowed step the model is told so** and asked to submit whatever it
is genuinely confident in, partial or not. Once it has actually looked at
some pivots, `tool_choice` is pinned to `submit_count` for that step - but
never before, since forcing an answer out of a model that has looked at
nothing just manufactures indices for the validator to reject. That
instruction appears in the transcript too, because it changes what the
model does. In practice the agent needs **6+ steps** (skeleton, finer
degree, measure, test, fix, answer); the UI says so, and above ~12 rate
limits become the binding constraint rather than the budget.

Rejected structures are shown with the rule they broke, next to a
transcript of every tool call the agent made - **with its arguments**,
so the transcript says which swings it listed and which legs it compared
rather than just naming verbs. The section is never hidden: "it made no
tool calls at all" is itself the finding when a model answers in prose
instead of working the chart, and an absent section reads as a rendering
bug rather than as that answer. The point is that you can audit *how* it
got there, not just what it concluded. It has its
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
   later want the AI tab to work, either set `T3_AI_API_KEY` on the service
   or let each user paste their own key into the browser - no other
   server-side config is needed,
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

# Run the automated test suite (420 tests)
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
