"""Dashboard backend (spec section 18/28).

Serves real OHLC candles plus Elliott/Fibonacci/BOS-CHoCH/entry-SL-TP
overlays for the frontend (static/index.html, TradingView lightweight-
charts) to draw directly on real candlesticks - never schematic
placeholder lines, per section 18's explicit requirement.

DATA SOURCE: Bybit only. Binance was the original source, but in
production it kept an IP-level ban on its REST API and its public
WebSocket completed the connection handshake yet delivered zero trades
indefinitely - a silent failure, not a real error, confirmed via the
trades_received counter staying at 0 for many minutes on a real deploy.
Rather than keep working around a exchange that won't serve this app's
traffic, every code path here now talks to Bybit exclusively (`market_data/
bybit_rest_client.py` for history/symbols, `market_data/bybit_ws_client.py`
for live). `market_data/rest_client.py` and `ws_client.py` (the Binance
clients) still exist and are still unit-tested, dormant rather than
deleted in case Binance access is restored later, but nothing in this
file calls them anymore.

By default `/api/run` replays the SYNTHETIC fixture (backtest/synthetic_
data.py) because this sandboxed build session cannot reach any real
exchange. Pass `?source=bybit&symbol=BTCUSDT` to pull real historical
klines instead - that code path is real, it just needs to run somewhere
with outbound network access to actually work.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Dict, List, Optional

import httpx
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from t3_engine.ai_advisor.advisor import AIAdvisorError, request_commentary
from t3_engine.backtest.engine import BacktestConfig, BacktestEngine
from t3_engine.backtest.metrics import compute_metrics, compute_metrics_by_wave
from t3_engine.backtest.synthetic_data import generate_synthetic_series
from t3_engine.common.models import Scenario
from t3_engine.common.types import TRADEABLE_TIMEFRAMES, Timeframe, WaveLabel
from t3_engine.dashboard.serialization import (
    candle_to_dict,
    pivot_to_dict,
    position_to_dict,
    scenario_to_dict,
    signal_to_dict,
    structure_event_to_dict,
    wave_to_dict,
)
from t3_engine.fibonacci.calculator import (
    wave2_levels,
    wave3_targets_from_wave2_end,
    wave4_levels,
    wave5_targets,
    wave_c_targets,
)
from t3_engine.market_data.bybit_rest_client import BybitAPIError, BybitFuturesREST
from t3_engine.market_data.fallback_symbols import FALLBACK_USDT_PERPETUAL_SYMBOLS
from t3_engine.pipeline.live_loop import LiveTradingEngine

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# Without this, `logging.getLogger(__name__).info(...)` calls anywhere in
# this app (live_loop.py, ws_client.py's connect/disconnect/trade-count
# messages) are silently swallowed - Python's root logger has NO handler
# by default, and gunicorn/uvicorn only configure their OWN loggers
# (uvicorn.access/uvicorn.error), not arbitrary application loggers. This
# was discovered by its absence: a batch of connection-visibility logging
# was added and shipped, then confirmed completely missing from Render's
# logs even though the code path definitely ran (the trades_received
# counter it stands next to is a plain field, not routed through logging,
# and it did show real values). basicConfig() here makes every module's
# logger actually reach stdout, which Render captures as app logs.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="T3 Elliott Wave Trading Engine Dashboard")

# --- live engine registry (spec section 20: one running pipeline per
# symbol, driven by the public Bybit WebSocket - no API key needed for
# market data). Kept as simple in-process state: this is a single-process
# dashboard, not a distributed deployment. ---
_live_engines: Dict[str, LiveTradingEngine] = {}
_live_tasks: Dict[str, asyncio.Task] = {}
_live_started_at: Dict[str, int] = {}
_live_errors: Dict[str, str] = {}


_SYMBOL_STRIP_RE = re.compile(r"[^A-Za-z0-9]")


def normalize_symbol(raw: str) -> str:
    """Exchange symbols are plain alphanumeric strings (e.g. `BTCUSDT`) -
    no slash, no space, no lowercase-vs-uppercase distinction. Users
    naturally type things like "BTC/USDT", "btc usdt" or paste a
    lowercase ticker; reject silently-wrong requests instead of sending
    garbage straight to Bybit (or, worse, to a half-typed partial string
    on every keystroke - see the frontend's debounce/commit-on-start fix
    for the other half of this)."""
    return _SYMBOL_STRIP_RE.sub("", raw).upper()


def bybit_error_message(exc: Exception) -> str:
    if isinstance(exc, BybitAPIError):
        return f"Bybit API returned an error: {exc}"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"Bybit API returned HTTP {exc.response.status_code}: {exc.response.text[:300]}"
    return f"Could not reach Bybit: {exc}"


async def _run_live_guarded(symbol: str, engine: LiveTradingEngine) -> None:
    try:
        await engine.run_live()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface it in /api/live/status instead of crashing the process
        _live_errors[symbol] = str(exc)


# Bumped on every deploy made purely to prove a deploy actually reached
# this running process - visible both here and as the yellow badge next
# to the title in index.html, so a user and a developer checking Render's
# logs/this endpoint can confirm they're looking at the same build without
# any ambiguity from browser/proxy caching.
BUILD_VERSION = "BUILD-CHECK-009"


@app.get("/api/health")
def health():
    return {"status": "ok", "build": BUILD_VERSION}


# --- symbol list cache: Bybit lists hundreds of linear perpetual symbols
# and that list barely changes minute to minute, so we cache it in-process
# instead of hitting instruments-info on every dashboard page load. ---
_symbols_cache: Dict[str, object] = {"symbols": None, "fetched_at": 0.0, "source": "live"}
_SYMBOLS_CACHE_TTL_SECONDS = 3600.0


@app.get("/api/symbols")
def list_symbols():
    now = time.time()
    if _symbols_cache["symbols"] is not None and (now - _symbols_cache["fetched_at"]) < _SYMBOLS_CACHE_TTL_SECONDS:
        return {"symbols": _symbols_cache["symbols"], "cached": True, "source": _symbols_cache["source"]}

    bybit = BybitFuturesREST()
    try:
        symbols = bybit.list_symbols()
        _symbols_cache["symbols"] = symbols
        _symbols_cache["fetched_at"] = now
        _symbols_cache["source"] = "live"
        return {"symbols": symbols, "cached": False, "source": "live"}
    except (httpx.HTTPStatusError, httpx.RequestError, BybitAPIError) as exc:
        # Never let the picker come back empty just because Bybit is
        # temporarily unreachable - fall back to the static list (see
        # fallback_symbols.py) instead of raising, so typing a letter
        # always suggests something real while we wait this out.
        return {"symbols": FALLBACK_USDT_PERPETUAL_SYMBOLS, "cached": False, "source": "fallback",
                "reason": bybit_error_message(exc)}
    finally:
        bybit.close()


@app.get("/api/run")
def run_backtest(source: str = Query("synthetic"), symbol: str = Query("SYNTHETIC"),
                  cycles: int = Query(2, ge=1, le=10), threshold: float = Query(60.0, ge=0, le=100),
                  limit: int = Query(1500, ge=100, le=10000), timeframe: str = Query("5m"),
                  equity: float = Query(10_000.0, gt=0, le=1_000_000_000)):
    try:
        tf = Timeframe(timeframe)
    except ValueError:
        raise HTTPException(400, f"Unsupported timeframe {timeframe}")

    if source == "bybit":
        symbol = normalize_symbol(symbol)
        if not symbol:
            raise HTTPException(400, "Symbol is empty after normalization - expected something like BTCUSDT")

        bybit = BybitFuturesREST()
        try:
            candles = bybit.get_klines(symbol, tf, limit=limit)
        except (httpx.HTTPStatusError, httpx.RequestError, BybitAPIError, ValueError) as exc:
            raise HTTPException(502, bybit_error_message(exc))
        finally:
            bybit.close()
    else:
        # The synthetic demo fixture is a fixed-shape 5m series (see
        # backtest/synthetic_data.py) - the timeframe picker is disabled for
        # this source in the frontend, and `tf` is only used below to tag
        # the engine's degree consistently with the candles it's fed.
        candles = generate_synthetic_series(num_cycles=cycles)
        symbol = symbol if symbol != "SYNTHETIC" else "SYNTHETIC-DEMO"
        tf = Timeframe.M5

    # "Unlimited capital" (a follow-up request) isn't a real lever in a
    # %-of-equity risk model: risk_per_wave is a FRACTION of equity, so
    # position sizing, PnL and drawdown all scale proportionally with
    # whatever `equity` is - a $10k account risking 1% behaves identically
    # (in R-multiple terms) to a $10M one risking 1%. What "unlimited"
    # honestly reduces to is "let me start from a much bigger number",
    # which this exposes directly rather than pretending capital caps were
    # ever a constraint on the strategy's own logic.
    engine = BacktestEngine(BacktestConfig(symbol=symbol, initial_equity=equity,
                                            entry_confidence_threshold=threshold, degree=tf))
    result = engine.run(candles)

    metrics = compute_metrics(result["closed_positions"])
    metrics_by_wave = compute_metrics_by_wave(result["closed_positions"])

    tf_note = (
        None if tf in TRADEABLE_TIMEFRAMES else
        f"{tf.value} is confirmation-only, not a tradeable degree "
        f"({', '.join(t.value for t in TRADEABLE_TIMEFRAMES)} are) - structure, wave counts and "
        "Fibonacci are shown as usual, but no signals/trades are evaluated here."
    )
    synthetic_note = (
        None if source == "bybit" else
        "Candles are a SYNTHETIC, clearly-labelled demo fixture (see backtest/synthetic_data.py) "
        "because this build environment cannot reach any real exchange. Pass source=bybit&symbol=... "
        "to use real historical data when running somewhere with normal internet access."
    )

    return {
        "symbol": symbol,
        "source": source,
        "data_source": source,
        "timeframe": tf.value,
        "candles": [candle_to_dict(c) for c in candles],
        "scenarios": [scenario_to_dict(s) for s in engine.scenario_engine.scenarios],
        "structure_events": [structure_event_to_dict(e) for e in engine.structure.events],
        # Full confirmed ZigZag swing history (not just the current
        # scenario's waves) - see pivot_to_dict for why: this is what lets
        # the chart show the whole swing structure leading up to today,
        # not just a handful of numbered waves floating with no context.
        "pivots": [pivot_to_dict(p) for p in engine.pivot_detector.pivots],
        # Every wave any top-ranked scenario ever confirmed over the whole
        # run (see ScenarioEngine.wave_history) - lets the chart number
        # waves across the ENTIRE loaded history, not just the handful the
        # current/latest scenario happens to still be holding.
        "wave_history": [
            wave_to_dict(w) for w in
            sorted(engine.scenario_engine.wave_history.values(), key=lambda w: w.start_timestamp)
        ],
        # Each motive (1/3/5) wave's own i-ii-iii-iv-v subdivision (see
        # BacktestEngine._update_subwaves / elliott_engine/scenario.py's
        # build_subwaves) - "waves and subwaves should be accounted for".
        "subwave_history": [
            wave_to_dict(w) for w in
            sorted(engine.subwave_history.values(), key=lambda w: w.start_timestamp)
        ],
        # Fibonacci projection for whichever wave is expected next - see
        # fibonacci_levels_for_scenario for why this is a persistent
        # overlay now, not just an accepted-signal's TP/SL lines.
        "fibonacci_levels": fibonacci_levels_for_scenario(
            engine.scenario_engine.scenarios[0] if engine.scenario_engine.scenarios else None
        ),
        "signals": [signal_to_dict(s) for s in result["signals"]],
        "closed_positions": [position_to_dict(p) for p in result["closed_positions"]],
        "open_positions": [position_to_dict(p) for p in result["open_positions"]],
        "final_equity": result["final_equity"],
        "metrics": {
            "overall": dataclass_metrics_to_dict(metrics),
            "by_wave": {k: dataclass_metrics_to_dict(v) for k, v in metrics_by_wave.items()},
        },
        "note": " ".join(n for n in (synthetic_note, tf_note) if n) or None,
    }


@app.post("/api/live/start")
async def start_live(symbol: str = Body(..., embed=True), equity: float = Body(10_000.0, embed=True),
                      threshold: float = Body(75.0, embed=True)):
    """Starts a live pipeline against Bybit's PUBLIC WebSocket stream
    (publicTrade) for `symbol` - no API key required, this is public
    market data, not account access. Runs in PAPER mode only."""
    symbol = normalize_symbol(symbol)
    if not symbol:
        raise HTTPException(400, "Symbol is empty after normalization - expected something like BTCUSDT")
    if symbol in _live_engines:
        return {"status": "already_running", "symbol": symbol}

    engine = LiveTradingEngine(symbol=symbol, initial_equity=equity, entry_confidence_threshold=threshold)
    _live_engines[symbol] = engine
    _live_errors.pop(symbol, None)
    _live_tasks[symbol] = asyncio.create_task(_run_live_guarded(symbol, engine))
    _live_started_at[symbol] = int(time.time() * 1000)
    return {"status": "started", "symbol": symbol}


@app.post("/api/live/stop")
async def stop_live(symbol: str = Body(..., embed=True)):
    symbol = normalize_symbol(symbol)
    task = _live_tasks.pop(symbol, None)
    _live_engines.pop(symbol, None)
    _live_started_at.pop(symbol, None)
    if task is not None:
        task.cancel()
        return {"status": "stopped", "symbol": symbol}
    return {"status": "not_running", "symbol": symbol}


@app.get("/api/live/status")
def live_status():
    return {
        symbol: {
            "running": symbol in _live_tasks and not _live_tasks[symbol].done(),
            "started_at": _live_started_at.get(symbol),
            "error": _live_errors.get(symbol),
            "candles_by_timeframe": {tf.value: len(hist) for tf, hist in engine.history.items()},
            # Proves real market data is arriving over the WS connection
            # well before the first 5m/15m candle actually closes - without
            # this there's no way to tell "connected but silent" apart from
            # "connected and receiving trades" for several real minutes.
            "trades_received": engine.trades_received,
            "live_source": engine.live_source,  # always "bybit" - see pipeline/live_loop.py
        }
        for symbol, engine in _live_engines.items()
    }


@app.get("/api/live/state")
def live_state(symbol: str = Query(...), timeframe: str = Query("5m")):
    symbol = normalize_symbol(symbol)
    engine = _live_engines.get(symbol)
    if engine is None:
        raise HTTPException(404, f"No live engine running for {symbol} - POST /api/live/start first")
    try:
        tf = Timeframe(timeframe)
    except ValueError:
        raise HTTPException(400, f"Unsupported timeframe {timeframe}")
    if tf not in engine.engines:
        raise HTTPException(400, f"{symbol} is not tracking {timeframe} (tracked: {[t.value for t in engine.engines]})")

    tf_engine = engine.engines[tf]
    candles = list(engine.history[tf])
    # The current, still-forming bar isn't in `history` yet (it only gets
    # appended once its bucket closes - see candle_builder/aggregator.py's
    # no-lookahead guarantee), but a live chart should still show it
    # updating in real time rather than sit empty until the first bar closes.
    forming = engine.candle_builder.current_candle(tf)
    if forming is not None:
        candles = candles + [forming]
    return {
        "symbol": symbol,
        "timeframe": tf.value,
        "candles": [candle_to_dict(c) for c in candles],
        "waiting_for_first_candle": len(candles) == 0,
        "trades_received": engine.trades_received,
        "live_source": engine.live_source,
        "error": _live_errors.get(symbol),
        "scenarios": [scenario_to_dict(s) for s in tf_engine.scenario_engine.scenarios],
        "structure_events": [structure_event_to_dict(e) for e in tf_engine.structure.events],
        "pivots": [pivot_to_dict(p) for p in tf_engine.pivot_detector.pivots],
        "wave_history": [
            wave_to_dict(w) for w in
            sorted(tf_engine.scenario_engine.wave_history.values(), key=lambda w: w.start_timestamp)
        ],
        "subwave_history": [
            wave_to_dict(w) for w in
            sorted(tf_engine.subwave_history.values(), key=lambda w: w.start_timestamp)
        ],
        "fibonacci_levels": fibonacci_levels_for_scenario(
            tf_engine.scenario_engine.scenarios[0] if tf_engine.scenario_engine.scenarios else None
        ),
        "tradeable": tf in TRADEABLE_TIMEFRAMES,
        "signals": [signal_to_dict(s) for s in tf_engine.signals[-50:]],
        "open_positions": [position_to_dict(p) for p in tf_engine.position_manager.positions.values() if not p.closed],
        "closed_positions": [position_to_dict(p) for p in tf_engine.position_manager.closed_positions],
        "equity": tf_engine.risk_manager.equity,
        "trading_enabled": tf_engine.risk_manager.trading_enabled,
    }


@app.post("/api/ai/advice")
def ai_advice(api_key: str = Body(..., embed=True), context: dict = Body(..., embed=True),
              model: str = Body("gpt-4o-mini", embed=True)):
    """BYO-key GPT second opinion (spec follow-up: 'подключить мой купленный
    ChatGPT'). The key is used for exactly one outbound request and never
    written to disk/DB/logs - see ai_advisor/advisor.py docstring for why
    this only ever produces commentary, never a trading decision."""
    try:
        result = request_commentary(api_key, context, model=model)
    except AIAdvisorError as exc:
        raise HTTPException(502, str(exc))
    return {"commentary": result.text, "model": result.model}


def dataclass_metrics_to_dict(m) -> dict:
    return {
        "trades": m.trades, "winrate": m.winrate, "profit_factor": m.profit_factor,
        "expectancy": m.expectancy, "sharpe": m.sharpe, "sortino": m.sortino,
        "max_drawdown": m.max_drawdown, "average_r": m.average_r, "median_r": m.median_r,
        "mae_avg": m.mae_avg, "mfe_avg": m.mfe_avg,
    }


def fibonacci_levels_for_scenario(scenario: Optional[Scenario]) -> List[Dict]:
    """Fibonacci retracement/extension levels for whichever wave is
    expected NEXT after the primary scenario's confirmed waves - the
    projection a real analyst draws once a wave completes, to mark where
    the next one is likely to go. Reuses the exact SAME functions
    signal_engine/targets.py uses to build real TP/SL levels, so this is
    never a separate, possibly-inconsistent "generic fib grid" - it's the
    actual numbers the strategy itself would act on if a signal fires here.

    Before this, the only Fibonacci-derived lines ever drawn on the chart
    were a signal's accepted TP/SL levels (drawTradeLevels in index.html) -
    with a strict confidence threshold and hard Elliott-rule gating,
    accepted signals are rare, so for long stretches of a session NO
    Fibonacci overlay appeared at all even though the scoring engine
    (score_fibonacci in elliott_engine/scenario.py) was using it the whole
    time internally. This exposes those same numbers as a persistent,
    always-visible overlay instead of only on a trade."""
    if scenario is None or not scenario.waves or scenario.next_expected_label is None:
        return []
    by_label = {w.label: w for w in scenario.waves}
    next_label = scenario.next_expected_label

    levels = None
    if next_label == WaveLabel.W2 and WaveLabel.W1 in by_label:
        w1 = by_label[WaveLabel.W1]
        levels = wave2_levels(w1.start_price, w1.end_price)
    elif next_label == WaveLabel.W3 and WaveLabel.W1 in by_label and WaveLabel.W2 in by_label:
        w1, w2 = by_label[WaveLabel.W1], by_label[WaveLabel.W2]
        levels = wave3_targets_from_wave2_end(w1.start_price, w1.end_price, w2.end_price)
    elif next_label == WaveLabel.W4 and WaveLabel.W3 in by_label:
        w3 = by_label[WaveLabel.W3]
        levels = wave4_levels(w3.start_price, w3.end_price)
    elif next_label == WaveLabel.W5 and WaveLabel.W1 in by_label and WaveLabel.W4 in by_label:
        w1, w4 = by_label[WaveLabel.W1], by_label[WaveLabel.W4]
        levels = wave5_targets(w1.start_price, w1.end_price, w4.end_price)
    elif next_label == WaveLabel.C and WaveLabel.A in by_label and WaveLabel.B in by_label:
        wa, wb = by_label[WaveLabel.A], by_label[WaveLabel.B]
        levels = wave_c_targets(wa.start_price, wa.end_price, wb.end_price)
    # A and B have no fib formula in this codebase (see fibonacci/calculator.py -
    # only wave2/3/4/5/C are spec-defined ratios), so next_label in (A, B)
    # correctly yields nothing rather than a made-up level.

    if levels is None:
        return []
    return [{"ratio": lvl.ratio, "price": lvl.price, "for_wave": next_label.value} for lvl in levels]


# Both routes below MUST NOT be cacheable by anything sitting between the
# browser and this process (the browser's own HTTP cache, a carrier/ISP
# compression proxy, etc.) - a stale HTTP-cached copy of either file would
# silently defeat the service-worker-update mechanism entirely, since the
# browser only detects a new service worker by diffing sw.js's bytes, and
# can't diff bytes it never actually re-fetched from the origin. This was
# observed in practice: server logs showed a phone fetching both files
# successfully seconds after a deploy went live, yet the already-open tab
# kept showing old UI text - explicit no-store headers plus the
# controllerchange auto-reload in index.html (see its <script>) close both
# ends of that gap.
_NO_CACHE_HEADERS = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}


@app.get("/")
def index():
    with open(os.path.join(_STATIC_DIR, "index.html"), encoding="utf-8") as f:
        html = f.read()
    html = html.replace("{{BUILD_VERSION}}", BUILD_VERSION)
    return HTMLResponse(html, headers=_NO_CACHE_HEADERS)


@app.get("/sw.js")
def service_worker():
    # Served from the root path (not /static/sw.js) so its default scope
    # covers the whole app - a service worker registered from /static/
    # could only ever control /static/* requests.
    return FileResponse(os.path.join(_STATIC_DIR, "sw.js"), media_type="application/javascript",
                         headers=_NO_CACHE_HEADERS)


app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
