# Current architecture — audit before the rework

Written by reading the code, not from memory. Every defect below is
stated with the file and the line of reasoning that makes it a defect,
and several of them are mine from the first build.

## 1. The map

| Concern | File | Entry point |
|---|---|---|
| WebSocket manager | `bybit_ws.py` | `BybitLeadStream.run()` — own asyncio loop on thread `lead-engine-ws`; reconnect 1→30s; `feed()` is the seam replay and tests use |
| Message routing | `engine.py` | `LeadEngine.handle_message(topic, msg)` → `parse_topic` → per-kind branch |
| Order book | `orderbook_engine.py` | `OrderBook.apply()` (snapshot/delta, sequence + crossed checks), `metrics()` |
| Trade flow | `trade_flow.py` | `TradeFlow.add()`, windows 250ms…5m, velocity z-score |
| CVD | `cvd.py` | `CvdState.add()`, four named price/flow relationships |
| Microprice | `microprice.py` | `MicropriceState.observe()`, deltas 250ms/1s/3s/5s |
| Open interest | `oi_engine.py` | `OpenInterestPoller` thread `lead-engine-oi` + `OpenInterestState` |
| BTC lead | `btc_leadlag.py` | `BtcLeadLag.state_for()`, 1s return grid, cross-correlation over 0–8 lags |
| SMC | `smc_engine.py` | `SmcEngine.state()`, fractal swings over closed bars |
| Elliott context | `elliott_state.py` | `ElliottContext.context()`, own machine or injected read-only reader |
| Pre-break | `prebreak_engine.py` | `LevelTracker` + `PreBreakEngine.evaluate(direction, inputs)` |
| Pressure | `pressure_engine.py` | `score(components, weights)` — nine flat components |
| Signals | `signal_machine.py` | `SignalMachine.update(SignalInputs)`, ten states |
| Health | `health.py` | `assess(StreamHealth, Thresholds)` |
| Per-symbol assembly | `state.py` | `SymbolState.snapshot()` |
| Persistence | `storage.py` | `Storage` buffered writer + `Recorder` thread |
| Replay | `replay.py` | `Replay.run()` on its own `LeadEngine` |
| HTTP | `api.py` | `APIRouter(prefix="/api/lead-engine")`, 11 routes |
| Frontend | `static/lead_engine.js` | one IIFE, global `leadEngineStore`; **no framework** — plain DOM |
| UI update | `static/lead_engine.js` | `setInterval(tick, 1000)` → `render()` → **`body.innerHTML = html`** |

There is **no React/Vue/Svelte anywhere in this project**. The dashboard
is a single hand-written page. Every instruction in the brief phrased as
"use `memo`/`useMemo`/selectors" therefore has to be met by its intent —
render the labels once, update only the values — rather than by its
letter.

## 2. Defects found

### A. Maths

**A1. `delta_ratio` can print 1,000,000.** `trade_flow.WindowFlow.delta_ratio`
is `buy / max(sell, 1e-9)`, clamped only at display time to `1e6`. It is
*not* in the pressure score (`TradeFlow.pressure_component` already uses
`delta / total`), so this is a display defect rather than a scoring one —
but it is on screen and it is meaningless. There is no
`normalized_delta` field at all.

**A2. Every normalisation constant is absolute, not rolling.** This is the
real one. `rolling.scale_to_unit(value, full_scale)` is used with
hardcoded full scales: microprice drift saturates at 6 bps
(`microprice.FULL_SCALE_BPS`), liquidations at 4× a fixed 25,000/s
(`config.Thresholds.liquidation_velocity`), OI at 2%
(`oi_engine.FULL_SCALE_PCT`). Six basis points of drift is enormous on
BTCUSDT and noise on DOGEUSDT. The only genuinely self-scaling features
are the trade-velocity z-score and `CvdState.pressure_component` (which
divides by its own observed range). Everything else compares instruments
on a scale that does not fit them.

**A3. No book alignment.** `OrderBook.pressure_component` is
`0.6 × weighted_obi + 0.4 × obi(5)`. OBI1 alone cannot dominate it — that
part of the brief's concern does not apply as written — but there is no
`top_book_score`, no `deep_book_score`, and nothing that notices when
OBI1 says +0.98 while OBI50 says −0.12. The five OBI depths are computed
and displayed and then two of them are averaged.

**A4. Absorption is a raw unbounded ratio and is not scored.**
`bid_absorption = added / executed` in `orderbook_engine.metrics()`. It
appears on screen as a number like 68815 and feeds nothing. There is no
z-score, no percentile, no 0–1 score.

**A5. Walls are tracked but not classified and not scored.** `Wall` has
`persistence_ms`, and cancellations are counted, but there is no
TRANSIENT / PERSISTENT / REPLENISHING / SPOOF / ABSORBED distinction and
no wall term in any score. A 10-second-old wall and a 10-minute-old wall
are the same object.

**A6. Pressure is one flat weighted sum of nine components**
(`pressure_engine.score`). There are no structure / flow / book /
derivatives / BTC layers, so a reading cannot be attributed to a layer and
layers cannot disagree.

**A7. `conflict` is a proxy, not a detector.** It is
`min(long_pressure, short_pressure)` — it notices that both sides scored,
but not *which independent sources* disagree. Structure bullish against
flow and book bearish produces the same number as nine mildly mixed
components.

**A8. `break_probability` is called a probability and is not one.** It is
a weighted mean of ten 0–1 features times a compression gate
(`prebreak_engine.evaluate`). Nothing has been calibrated against what
actually happened. The field name, the API and the UI all say
"probability".

### B. Pre-break

**B1. The "no level" message is wrong for resistance.**
`prebreak_engine.evaluate` returns
`f"no {kind} identified below the visible swings"` for both directions.
The *logic* in `LevelTracker.nearest` is correct — support is filtered to
`price <= current`, resistance to `price >= current` — so this is a
message defect, and the reported symptom ("no resistance identified below
visible swings") is that message and not a search in the wrong direction.
It still needs fixing and a test, because the message is what a user
reads.

**B2. Features are computed but only partly surfaced.** `evaluate`
returns all ten, and the API returns them, but there is no per-direction
breakdown of *which* are missing versus zero.

### C. UI

**C1. The whole panel is rebuilt every second.** `render()` composes one
HTML string for eleven cards and assigns `body.innerHTML = html`. Every
label, every bar and every number is destroyed and recreated once a
second. That is the visible jitter, and it is also why text shifts: the
numbers are proportional-width.

**C2. No `tabular-nums` anywhere.** `lead_engine.css` never sets
`font-variant-numeric`, so `5.9470` and `5.9510` occupy different widths.

**C3. Colour flips on any sign change.** `tone()` returns up/down on
`n > 0` / `n < 0` with no deadband, so a value oscillating around zero
strobes green/red.

**C4. Calculation and display share one clock.** `POLL_MS = 1000` both
fetches and repaints; there is no separation of calculation frequency from
display refresh.

### D. Latency and health

**D1. One number is reported as two different things.**
`health.latency_ms` is the median of `local_now − exchange_ts` over the
last 200 frames (`bybit_ws.StreamStats.note_latency`). The tab prints it
as "latency" *and* separately prints `last_book_age_s` as "book age".
When the header showed `latency 3334ms` next to `book 3.3s` those were
the same staleness twice. There is no separate network latency, no
processing latency on screen, and no UI latency at all.

**D2. Stale data does already mute signals** (`health.assess` →
`signals_enabled=False` → `DATA_FAILURE`), and `max_book_age_seconds` is
5s. But the header says "feed OK" from `health.status` while showing a
multi-second book age, because the age is printed unconditionally
alongside a status that was computed from it.

### E. Missing entirely

No per-symbol workspace or route. No chart of any kind — the Lead Engine
tab hides the shared `#analystChart` and draws nothing. No EMA. No
Fibonacci tool. No signal markers. No candle REST endpoint. No external
API version, snapshot endpoint, stream, auth or MCP adapter.

## 3. What is sound and should not be disturbed

* The sequence and crossed-book checks, and the refusal to serve metrics
  from an untrusted book.
* `note_trade` separating executed from cancelled size.
* Exchange-timestamp windows throughout `rolling.py`.
* Closed-candles-only structure, and `_future()` in replay — the
  no-lookahead guarantees.
* The isolation boundary and its tests.
* The health gate muting signals.
