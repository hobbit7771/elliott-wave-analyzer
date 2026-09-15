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
from sqlalchemy.exc import SQLAlchemyError

from t3_engine.ai_advisor.advisor import (
    DEFAULT_API_BASE as DEFAULT_AI_API_BASE,
    DEFAULT_API_KEY as SERVER_AI_KEY,
    DEFAULT_MODEL as DEFAULT_AI_MODEL,
    DEFAULT_READ_TIMEOUT,
    MAX_READ_TIMEOUT,
    PROVIDER_NAME,
    AIAdvisorError,
    check_access as ai_check_access,
    diagnose as ai_diagnose,
    probe_variants as ai_probe_variants,
    ping as ai_ping,
    request_commentary,
    request_wave_count,
    resolve_api_key,
)
from t3_engine.ai_advisor.analyst import DEFAULT_MAX_STEPS, MAX_MAX_STEPS, run_analyst
from t3_engine.ai_advisor import analysis_store, deep_count, jobs, trade_journal
from t3_engine.ai_advisor.multi_timeframe import run_multi_timeframe
from t3_engine.ai_advisor.target_odds import annotate_projection
from t3_engine.ai_advisor.usage import UsageMeter, cost_of, fetch_pricing, monthly_estimate
from t3_engine.ai_advisor.relational import (
    DEFAULT_RELATIONAL_MODEL,
    DEFAULT_RELATIONAL_URL,
    RelationalUnavailable,
    predict_trade_quality,
    rows_from_backtest,
)
from t3_engine.ai_advisor.ai_trading import grid_scenario, subwaves_for
from t3_engine.ai_advisor.analyst_tools import ToolError
from t3_engine.backtest.engine import BacktestConfig, BacktestEngine
from t3_engine.backtest.metrics import compute_metrics, compute_metrics_by_wave
from t3_engine.backtest.synthetic_data import (
    aggregate_candles,
    generate_synthetic_series_for,
)
from t3_engine.common.models import Scenario
from t3_engine.common.types import TRADEABLE_TIMEFRAMES, Direction, Timeframe, WaveLabel
from t3_engine.elliott_engine.external_count import ExternalCountRejected, validate_external_count
from t3_engine.market_structure.pivots import ZigZagPivotDetector
from t3_engine.market_structure.structure import MarketStructureTracker
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
from t3_engine.database import candle_store, supabase_rest
from t3_engine.database.session import is_durable
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

logger = logging.getLogger(__name__)

app = FastAPI(title="T3 Elliott Wave Trading Engine Dashboard")

# --- Market Lead Engine -------------------------------------------------
# A separate realtime microstructure engine that shares this process and
# nothing else. It is mounted here and started below; those two calls plus
# one tab in index.html are the ENTIRE contact surface between it and this
# file. Everything it does lives in t3_engine/lead_engine/, it reaches
# none of the code in this module, and this module calls only its facade.
#
# The import is unconditional but inert: importing the package starts
# nothing (see lead_engine/__init__.py), and with LEAD_ENGINE_ENABLED
# unset every route answers {"enabled": false} and no thread exists.
from t3_engine.lead_engine import api as lead_engine_api          # noqa: E402
from t3_engine.lead_engine import api_v1 as lead_engine_api_v1    # noqa: E402
from t3_engine.lead_engine import config as lead_engine_config    # noqa: E402
from t3_engine.lead_engine.engine import get_engine as get_lead_engine  # noqa: E402

app.include_router(lead_engine_api.router)


@app.get("/lead-engine/{symbol}", response_class=HTMLResponse)
def lead_engine_workspace(symbol: str):
    """The Market Workspace for one instrument.

    Its own page, not a tab: a full-height chart with the Lead Engine
    panel beside it. Served from this file because the dashboard owns the
    web process, but every byte it loads - the markup, the styles, the
    chart controller, the Fibonacci tool - lives in
    static/lead_workspace.* and static/lead_*.js. Nothing in the
    dashboard's own page is touched by it.

    The symbol is in the PATH so the page can be linked, bookmarked and
    opened in its own tab, which is what "open a separate workspace for
    this coin" needs to mean to be useful."""
    if not lead_engine_config.enabled():
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'>"
            "<body style='background:#0e1117;color:#fde68a;font:14px system-ui;padding:24px'>"
            "<h2>Market Lead Engine is switched off</h2>"
            f"<p>Set <code>{lead_engine_config.ENABLED_ENV}=true</code> and redeploy.</p>"
            "<p><a style='color:#7dd3fc' href='/'>Back to the dashboard</a></p></body>",
            status_code=200)
    path = os.path.join(_STATIC_DIR, "lead_workspace.html")
    if not os.path.exists(path):
        raise HTTPException(404, "workspace page is missing from this build")
    with open(path, "r", encoding="utf-8") as handle:
        return HTMLResponse(handle.read())
# The versioned, token-gated, read-only external surface. Mounted always;
# every route inside refuses unless EXTERNAL_AI_ACCESS_ENABLED is true AND
# a valid LEAD_ENGINE_API_KEY is presented - see lead_engine/auth.py.
app.include_router(lead_engine_api_v1.router)

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
# Maximum reasoning power by default, on the project owner's instruction.
# An empty string still means "do not send the parameter at all", which is
# what a model that has never heard of it needs - so the default is a real
# value here and the UI keeps "Not sent" as an explicit choice.
DEFAULT_REASONING_EFFORT = "max"

BUILD_VERSION = "BUILD-CHECK-043"


@app.get("/api/health")
def health():
    # Durable when EITHER backend is a real database: Supabase over REST
    # (the credential that is actually obtainable - see
    # database/supabase_rest.py) or a Postgres URL in T3_DATABASE_URL.
    using_supabase = supabase_rest.configured()
    durable = using_supabase or is_durable(analysis_store.DEFAULT_DATABASE_URL)
    return {
        "status": "ok", "build": BUILD_VERSION,
        # Saved counts and the trade journal live in this database. On the
        # default SQLite file that is the CONTAINER filesystem, which is
        # replaced on every deploy - so every labelled chart is erased by
        # the next push. Reported rather than left to be discovered.
        "storage_durable": durable,
        "storage_backend": "supabase-rest" if using_supabase else (
            "postgres" if durable else "sqlite-ephemeral"),
        "storage_note": "" if durable else
            "Saved analyses and the trade journal are in a SQLite file on the container "
            "filesystem and will be erased by the next deploy. Set T3_DATABASE_URL to a "
            "Postgres URL to keep them.",
    }


# --- symbol list cache: Bybit lists hundreds of linear perpetual symbols
# and that list barely changes minute to minute, so we cache it in-process
# instead of hitting instruments-info on every dashboard page load. ---
_symbols_cache: Dict[str, object] = {"symbols": None, "fetched_at": 0.0, "source": "live"}
_SYMBOLS_CACHE_TTL_SECONDS = 3600.0


@app.get("/api/ai/config")
def ai_config():
    """What the AI tab needs to render itself, and nothing secret.

    `server_key` is a BOOLEAN, never the key. The UI uses it to say "you can
    leave the key field empty" instead of making the user guess why a blank
    field sometimes works."""
    return {
        "server_key": bool(SERVER_AI_KEY),
        "model": DEFAULT_AI_MODEL,
        "base_url": DEFAULT_AI_API_BASE,
        "provider": PROVIDER_NAME,
    }


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


def load_candles(source: str, symbol: str, timeframe: str, limit: int, cycles: int):
    """Resolve a data request into (candles, symbol, timeframe).

    Shared by /api/run and /api/ai/label so the AI labelling path analyses
    the EXACT same series - and therefore the exact same server-computed
    pivots - that the deterministic engine does. If the two loaded data
    differently, "the server validated the model's indices" would be a
    claim about a different chart than the one on screen."""
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
        # The synthetic demo fixture is generated AT the requested
        # timeframe now (5m bars aggregated up, exactly as an exchange
        # builds a 4h bar from its 5m ones). It used to return the same 5m
        # series whatever was asked for, which made a multi-timeframe run
        # analyse one chart four times and then reconcile it with itself -
        # the model spotted it before anyone else and wrote "the supplied
        # data repeats 5m" into its own verdict.
        candles = generate_synthetic_series_for(tf, num_cycles=cycles)
        symbol = symbol if symbol != "SYNTHETIC" else "SYNTHETIC-DEMO"

    return candles, symbol, tf


@app.get("/api/run")
def run_backtest(source: str = Query("synthetic"), symbol: str = Query("SYNTHETIC"),
                  cycles: int = Query(2, ge=1, le=10), threshold: float = Query(60.0, ge=0, le=100),
                  limit: int = Query(1500, ge=100, le=10000), timeframe: str = Query("5m"),
                  equity: float = Query(10_000.0, gt=0, le=1_000_000_000)):
    candles, symbol, tf = load_candles(source, symbol, timeframe, limit, cycles)

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
        # THE single globally consistent chain of confirmed structures (see
        # ScenarioEngine.confirmed_chain) - at most one wave covers any
        # given moment, so re-anchoring truncates and re-extends rather
        # than layering a second contradictory reading over the same
        # candles. The current, still-developing count is `scenarios[0]`.
        "confirmed_chain": [wave_to_dict(w) for w in engine.scenario_engine.confirmed_chain],
        # Each motive (1/3/5) chain wave's own i-ii-iii-iv-v subdivision
        # (see BacktestEngine._update_subwaves / build_subwaves), pruned
        # alongside the chain so a subwave never outlives its parent.
        "subwave_history": [wave_to_dict(w) for w in engine.subwave_history],
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


def seed_live_history(engine: LiveTradingEngine, symbol: str, bars: int) -> Dict[str, int]:
    """Backfill every timeframe the live engine tracks, from Bybit REST.

    Failures here are not fatal and are not hidden: the stream still works
    without a past, so a backfill that cannot be fetched degrades the
    session rather than refusing to start it. What comes back says how many
    candles each timeframe actually got, so an empty one is visible rather
    than assumed."""
    if bars <= 0:
        return {}
    filled: Dict[str, int] = {}
    bybit = BybitFuturesREST()
    try:
        for timeframe in engine.trading_timeframes:
            try:
                candles = bybit.get_klines(symbol, timeframe, limit=bars)
            except (httpx.HTTPStatusError, httpx.RequestError, BybitAPIError, ValueError) as exc:
                logger.warning("[live %s] could not backfill %s: %s", symbol, timeframe.value, exc)
                filled[timeframe.value] = 0
                continue
            filled[timeframe.value] = engine.seed_history(timeframe, candles)
    finally:
        bybit.close()
    return filled


@app.post("/api/live/start")
async def start_live(symbol: str = Body(..., embed=True), equity: float = Body(10_000.0, embed=True),
                      threshold: float = Body(75.0, embed=True),
                      backfill: int = Body(1000, embed=True, ge=0, le=5000)):
    """Starts a live pipeline against Bybit's PUBLIC WebSocket stream
    (publicTrade) for `symbol` - no API key required, this is public
    market data, not account access. Runs in PAPER mode only.

    The session is SEEDED from REST history first. Without that a live
    chart begins empty and with no past: pivots, the confirmed chain and
    every scenario would have to be rediscovered from candles arriving
    after the connection, which on 1h is hours away and on 4h is days. The
    seeded candles go through the same path a live one takes, so the
    no-lookahead guarantee is unchanged - each is processed knowing only
    the ones before it."""
    symbol = normalize_symbol(symbol)
    if not symbol:
        raise HTTPException(400, "Symbol is empty after normalization - expected something like BTCUSDT")
    if symbol in _live_engines:
        return {"status": "already_running", "symbol": symbol}

    engine = LiveTradingEngine(symbol=symbol, initial_equity=equity, entry_confidence_threshold=threshold)
    backfilled = seed_live_history(engine, symbol, backfill)
    _live_engines[symbol] = engine
    _live_errors.pop(symbol, None)
    _live_tasks[symbol] = asyncio.create_task(_run_live_guarded(symbol, engine))
    _live_started_at[symbol] = int(time.time() * 1000)
    return {"status": "started", "symbol": symbol, "backfilled": backfilled}


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


def live_ai_analysis(symbol: str, timeframe: Timeframe, candles: List) -> Optional[Dict]:
    """The stored analysis for a live series, with its age in candles.

    Age matters more than the analysis: a count made twelve 5m candles ago
    may be perfectly good or may have been invalidated by the very next
    bar, and only showing how stale it is lets anyone tell which question
    to ask. The cache is keyed on the same (source, symbol, timeframe) the
    history tab writes, so an analysis run there shows up here."""
    cached = analysis_store.load("bybit", symbol, timeframe.value)
    if cached is None:
        return None
    newer = sum(1 for candle in candles if candle.open_time > cached.last_candle_time)
    payload = cached.payload
    return {
        "accepted": payload.get("accepted", []),
        "projection": payload.get("projection"),
        "coverage": payload.get("coverage", {}),
        "summary": payload.get("summary", ""),
        "model": cached.model,
        "analysed_at": cached.created_at,
        "candles_since": newer,
        "stale": newer > 0,
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
    # Hand this timeframe's engine whatever count the agent has saved for
    # it, before reporting state. Idempotent by fingerprint, so polling
    # every few seconds costs nothing and a re-analysis takes effect on the
    # next candle that closes - never retroactively over the history the
    # agent already saw.
    analysis = live_ai_analysis(symbol, tf, candles)
    engine.apply_ai_analysis(tf, analysis)
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
        # In AI-only mode (the default for a live session) the chart is the
        # AGENT'S read of the market and nothing else. The engine's own
        # pivots, scenarios, confirmed chain, subwaves and Fibonacci grid
        # are still computed - the entry score needs the structure - but
        # they are not sent, because a second count drawn underneath the
        # agent's is exactly the superimposed mess the analyst tab exists
        # to avoid. `scenarios` carries the AI's count instead, so the
        # existing debug panel reads the thing actually being traded.
        "scenarios": ([scenario_to_dict(tf_engine.ai_scenario)] if tf_engine.ai_scenario else []
                      ) if engine.ai_only else
                     [scenario_to_dict(s) for s in tf_engine.scenario_engine.scenarios],
        "structure_events": [] if engine.ai_only else
                            [structure_event_to_dict(e) for e in tf_engine.structure.events],
        "pivots": [] if engine.ai_only else
                  [pivot_to_dict(p) for p in tf_engine.pivot_detector.pivots],
        "confirmed_chain": [] if engine.ai_only else
                           [wave_to_dict(w) for w in tf_engine.scenario_engine.confirmed_chain],
        # Subwaves and the Fibonacci grid are derived from the AGENT'S
        # count in ai_only mode, not from the engine's parallel one. A
        # finer degree drawn from a different reading than the labels above
        # it is not extra detail, it is a contradiction on the same candles.
        "subwave_history": [wave_to_dict(w) for w in
                            subwaves_for(tf_engine.ai_scenario, candles)] if engine.ai_only else
                           [wave_to_dict(w) for w in tf_engine.subwave_history],
        "fibonacci_levels": fibonacci_levels_for_scenario(grid_scenario(tf_engine.ai_scenario))
                            if engine.ai_only else fibonacci_levels_for_scenario(
            tf_engine.scenario_engine.scenarios[0] if tf_engine.scenario_engine.scenarios else None
        ),
        "ai_only": engine.ai_only,
        # Every stop and take-profit LEG that filled, in order. Without
        # this a position with two of its four legs filled showed as
        # "open: 1, closed: 0" and nothing else - the fills were real and
        # nothing on screen said so.
        "fills": [f for f in engine.fills if f["timeframe"] == tf.value][-50:],
        "trade_record": trade_journal.summary_for("bybit", symbol, tf.value),
        "seeded_candles": engine.seeded.get(tf, 0),
        # The saved AI analysis for this exact series, so a live chart opens
        # with the model's markup already on it instead of a bare stream.
        # `candles_since` is how far behind it has fallen - a saved count
        # shown without that would read as current when it is not.
        "ai_analysis": analysis,
        "tradeable": tf in TRADEABLE_TIMEFRAMES,
        "signals": [signal_to_dict(s) for s in tf_engine.signals[-50:]],
        "open_positions": [position_to_dict(p) for p in tf_engine.position_manager.positions.values() if not p.closed],
        "closed_positions": [position_to_dict(p) for p in tf_engine.position_manager.closed_positions],
        "equity": tf_engine.risk_manager.equity,
        "trading_enabled": tf_engine.risk_manager.trading_enabled,
    }


@app.post("/api/ai/advice")
def ai_advice(context: dict = Body(..., embed=True), api_key: str = Body("", embed=True),
              model: str = Body(DEFAULT_AI_MODEL, embed=True),
              base_url: str = Body(DEFAULT_AI_API_BASE, embed=True),
              timeout: float = Body(DEFAULT_READ_TIMEOUT, embed=True, gt=0, le=MAX_READ_TIMEOUT),
              reasoning_effort: str = Body(DEFAULT_REASONING_EFFORT, embed=True),
              thinking: str = Body("on", embed=True)):
    """BYO-key OrcaRouter second opinion. The key is used for exactly one
    outbound request and never written to disk/DB/logs - see
    ai_advisor/advisor.py's docstring for why this only ever produces
    commentary, never a trading decision."""
    try:
        result = request_commentary(resolve_api_key(api_key), context, model=model,
                                    base_url=base_url, timeout=timeout,
                                    reasoning_effort=reasoning_effort,
                                    thinking=parse_thinking(thinking))
    except AIAdvisorError as exc:
        raise HTTPException(502, str(exc))
    return {"commentary": result.text, "model": result.model}


@app.post("/api/ai/label")
def ai_label(api_key: str = Body("", embed=True), source: str = Body("synthetic", embed=True),
             symbol: str = Body("SYNTHETIC", embed=True), timeframe: str = Body("5m", embed=True),
             limit: int = Body(1500, embed=True, ge=100, le=10000),
             cycles: int = Body(2, embed=True, ge=1, le=10),
             model: str = Body(DEFAULT_AI_MODEL, embed=True),
             base_url: str = Body(DEFAULT_AI_API_BASE, embed=True),
             timeout: float = Body(DEFAULT_READ_TIMEOUT, embed=True, gt=0, le=MAX_READ_TIMEOUT),
             reasoning_effort: str = Body(DEFAULT_REASONING_EFFORT, embed=True),
             thinking: str = Body("on", embed=True)):
    """AI wave-labelling mode: the model proposes a count over the WHOLE
    loaded history - the one thing the deterministic engine deliberately
    won't do, since it only ever anchors on recent pivots.

    The model's answer never reaches the chart unchecked. This endpoint
    recomputes the pivots ITSELF from the same series /api/run uses (via
    load_candles + the same ZigZag detector and deviation), hands the
    model only indices into that server-owned list, and then runs whatever
    comes back through elliott_engine/external_count.py - existence, order,
    contiguity, alternation, causality, and the same hard Elliott rules the
    internal engine enforces. A mathematically impossible wave cannot be
    drawn by this path: it comes back as `valid: false` with the rule it
    broke, which is a useful answer rather than a silent failure.

    Note what is NOT accepted from the client: pivots. If the caller could
    supply those, "the server validated the indices" would be a statement
    about the caller's own data rather than about the chart."""
    candles, symbol, tf = load_candles(source, symbol, timeframe, limit, cycles)

    config = BacktestConfig(symbol=symbol, degree=tf)
    detector = ZigZagPivotDetector(deviation_pct=config.pivot_deviation_pct)
    structure = MarketStructureTracker(min_break_pct=config.structure_min_break_pct)
    for index, candle in enumerate(candles):
        found = detector.update(index, candle)
        if found is not None:
            structure.on_pivot(found)
    pivots = detector.pivots
    direction = structure.trend or Direction.UP

    if len(pivots) < 2:
        raise HTTPException(
            422,
            f"Only {len(pivots)} confirmed swing pivots in this history - not enough to count waves over. "
            "Load more candles or pick a lower timeframe.",
        )

    try:
        proposal = request_wave_count(
            resolve_api_key(api_key),
            [{"index": i, "time": p.timestamp // 1000, "price": p.price, "kind": p.kind}
             for i, p in enumerate(pivots)],
            direction.value,
            model=model,
            base_url=base_url,
            timeout=timeout,
            reasoning_effort=reasoning_effort,
            thinking=parse_thinking(thinking),
        )
    except AIAdvisorError as exc:
        raise HTTPException(502, str(exc))

    try:
        validated = validate_external_count(pivots, proposal.waves, direction, tf)
    except ExternalCountRejected as exc:
        # Structurally impossible - not a count at all. 200 with an
        # explicit rejection rather than a 4xx: the request was fine, the
        # model's answer wasn't, and the UI needs to say which.
        return {
            "valid": False,
            "rejected": True,
            "reason": str(exc),
            "model": proposal.model,
            "reasoning": proposal.reasoning,
            "proposed_raw": proposal.waves,
            "waves": [],
        }

    return {
        "valid": validated.valid,
        "rejected": False,
        "broken_rule": validated.broken_rule,
        "reason": validated.notes,
        "model": proposal.model,
        "reasoning": proposal.reasoning,
        "direction": direction.value,
        "pivot_count": len(pivots),
        "waves": [wave_to_dict(w) for w in validated.waves],
    }


@app.post("/api/ai/ping")
def ai_ping_endpoint(api_key: str = Body("", embed=True),
                     model: str = Body(DEFAULT_AI_MODEL, embed=True),
                     base_url: str = Body(DEFAULT_AI_API_BASE, embed=True),
                     timeout: float = Body(DEFAULT_READ_TIMEOUT, embed=True, gt=0, le=MAX_READ_TIMEOUT)):
    """One tiny round trip to the model, to tell a broken SETUP apart from
    a slow JOB.

    These look identical from the dashboard - a wrong key, a wrong base
    URL, a retired model id and a model that simply queues for four minutes
    all present as "nothing happened, then an error". This asks for a
    single token: if it comes back, the key, the URL and the model id are
    all correct and any later failure is about how long the real work
    takes. If it doesn't, the error names which of the three is wrong."""
    try:
        return ai_ping(resolve_api_key(api_key), model=model, base_url=base_url, timeout=timeout)
    except AIAdvisorError as exc:
        # 200 with ok:false, not a 5xx: the REQUEST was fine, the answer is
        # "your setup doesn't work and here is which part". A 502 here would
        # read in the UI as "the dashboard is broken".
        return {"ok": False, "error": str(exc), "model": model}


def parse_thinking(raw: str):
    """Tri-state, because "do not send this field at all" is a distinct and
    necessary choice: a model that has never heard of chat_template_kwargs
    answers 400 rather than ignoring it."""
    value = (raw or "").strip().lower()
    if value in ("on", "true", "1", "yes"):
        return True
    if value in ("off", "false", "0", "no"):
        return False
    return None


@app.post("/api/ai/signal-quality")
def ai_signal_quality(api_key: str = Body("", embed=True), source: str = Body("synthetic", embed=True),
                      symbol: str = Body("SYNTHETIC", embed=True),
                      timeframe: str = Body("5m", embed=True),
                      limit: int = Body(1500, embed=True, ge=100, le=10000),
                      cycles: int = Body(2, embed=True, ge=1, le=10),
                      equity: float = Body(10_000.0, embed=True, gt=0, le=1e9),
                      model: str = Body(DEFAULT_RELATIONAL_MODEL, embed=True),
                      url: str = Body(DEFAULT_RELATIONAL_URL, embed=True),
                      timeout: float = Body(120.0, embed=True, gt=0, le=MAX_READ_TIMEOUT)):
    """How often did setups scoring like this one actually work out?

    A different kind of model from everything else in the AI tab:
    `kumo-relational` takes a relational schema plus rows and returns a
    probability per row. It has no text output and no tool calling, so it
    cannot label waves or replace the chat model - but the engine already
    produces exactly the table it wants, since every signal carries its
    eight score components and every closed trade carries its outcome.

    Strictly advisory, like the second opinion. It never gates a trade,
    never moves a stop and never edits a count - the hard Elliott rules and
    the risk engine decide, and a model fitted to a few dozen of the
    engine's own past trades is a hint, not an edge.

    422 rather than a number when the history cannot answer honestly: too
    few closed trades, or all of them the same outcome. A probability from
    four trades would be believed, and should not be."""
    candles, symbol, tf = load_candles(source, symbol, timeframe, limit, cycles)
    config = BacktestConfig(symbol=symbol, degree=tf, initial_equity=equity)
    engine = BacktestEngine(config)
    engine.run(candles)

    context, predict = rows_from_backtest(
        engine.signals, engine.position_manager.closed_positions, tf.value)
    try:
        result = predict_trade_quality(resolve_api_key(api_key), context, predict,
                                       model=model, url=url, timeout=timeout)
    except RelationalUnavailable as exc:
        raise HTTPException(422, str(exc))
    except AIAdvisorError as exc:
        raise HTTPException(502, str(exc))

    return {
        "model": result.model,
        "context_trades": result.context_trades,
        "wins_in_context": result.wins_in_context,
        "scored": [{"signal_id": p.signal_id, "win_probability": p.win_probability,
                    "prediction": p.prediction} for p in result.predictions],
    }


# The saved multi-timeframe verdict is stored in the SAME cache the
# per-timeframe counts use, under this timeframe key. It is not a
# timeframe, it is the reconciliation OF the timeframes - kept there
# because that cache is what survives a restart, a closed tab and a
# dropped connection, which is exactly what a twelve-minute run must not
# be allowed to lose.
MTF_CACHE_KEY = "mtf-verdict"

# Saved counts are listed shortest timeframe first, the way the timeframe
# picker reads, rather than by when they happened to be computed.
TIMEFRAME_ORDER = {"1m": 0, "5m": 1, "15m": 2, "1h": 3, "4h": 4}


def run_cost(usage: Dict, api_key: str, model: str, base_url: str) -> Dict:
    """Token counts plus a dollar figure when one can be established.

    The estimate below is deliberately parametric: it says what ONE run
    cost and what a given number of runs per hour would come to, and it
    names the run rate rather than assuming one silently. Continuous
    monitoring is just a run rate - four analyses an hour is a different
    bill from forty, and only the caller knows which they mean."""
    out = dict(usage or {})
    if not out.get("total_tokens"):
        return out
    meter = UsageMeter(
        calls=out.get("calls", 0), prompt_tokens=out.get("prompt_tokens", 0),
        completion_tokens=out.get("completion_tokens", 0),
        reasoning_tokens=out.get("reasoning_tokens", 0),
        cached_tokens=out.get("cached_tokens", 0),
    )
    pricing = fetch_pricing(api_key, model, base_url)
    cost = cost_of(meter, pricing)
    out["pricing_known"] = pricing is not None
    if cost is not None:
        out["cost_usd"] = round(cost, 6)
        # One representative rate, stated as an assumption rather than a
        # prediction: a 5m chart produces a new candle twelve times an hour,
        # and re-analysing on every one of them is the busiest sane cadence.
        out["if_run_hourly"] = monthly_estimate(cost, 1.0)
        out["if_run_every_5m"] = monthly_estimate(cost, 12.0)
    return out


def run_multi_work(api_key: str, source: str, symbol: str, limit: int, cycles: int, model: str,
                   base_url: str, timeout: float, thinking: str, max_steps: int, force: bool,
                   on_progress=None) -> Dict:
    """One multi-timeframe run, as a plain function.

    Shared by the synchronous endpoint and the background job so the two
    cannot drift: the job is the same work with somewhere to report
    progress and somewhere to survive."""
    result = run_multi_timeframe(
        resolve_api_key(api_key), load_candles, source, symbol,
        limit=limit, cycles=cycles, model=model, force=force,
        max_steps=max_steps, base_url=base_url, timeout=timeout,
        thinking=parse_thinking(thinking), on_progress=on_progress,
    )
    body = {
        "symbol": result.symbol,
        "model": result.model,
        "note": result.note,
        "reused": result.reused_timeframes,
        "recomputed": result.recomputed_timeframes,
        "verdict": result.verdict,
        "timeframes": [
            {"timeframe": a.timeframe, "reused": a.reused, "candles": a.candles,
             "coverage": a.coverage, "summary": a.summary, "error": a.error,
             "steps_used": a.steps_used, "structures": len(a.accepted),
             "accepted": a.accepted, "projection": a.projection}
            for a in result.per_timeframe
        ],
    }
    newest = max((a.last_candle_time for a in result.per_timeframe), default=0)
    if any(a.accepted for a in result.per_timeframe):
        try:
            analysis_store.save(source, result.symbol, MTF_CACHE_KEY, newest,
                                sum(a.candles for a in result.per_timeframe), body,
                                model=result.model)
        except SQLAlchemyError as exc:
            logger.warning("could not cache the multi-timeframe verdict for %s: %s", symbol, exc)
    return body


def run_analyst_work(api_key: str, source: str, symbol: str, timeframe: str, limit: int,
                     cycles: int, model: str, base_url: str, timeout: float,
                     reasoning_effort: str, thinking: str, max_steps: int,
                     on_progress=None) -> Dict:
    """One analyst run, as a plain function - see run_multi_work."""
    candles, symbol, tf = load_candles(source, symbol, timeframe, limit, cycles)
    # File the series BEFORE the run, not after. A run that fails still
    # leaves behind the exact chart it failed on, which is the difference
    # between being able to look at what happened and having to re-fetch a
    # window that is no longer the same one. Non-fatal by construction -
    # see database/candle_store.py.
    candle_store.save(symbol, tf.value, candles)

    # What the paper trades opened from previous counts of THIS chart
    # actually did. Stated to the agent as history, never as a steer - see
    # trade_journal.brief_line.
    record = trade_journal.summary_for(source, symbol, tf.value)
    result = run_analyst(resolve_api_key(api_key), candles, tf, symbol=symbol, model=model,
                         max_steps=max_steps, base_url=base_url, timeout=timeout,
                         reasoning_effort=reasoning_effort,
                         thinking=parse_thinking(thinking), on_progress=on_progress,
                         record_line=trade_journal.brief_line(record, symbol, tf.value))

    projection = annotate_projection(result.projection, candles)
    # Save it under the SAME key the multi-timeframe view and the live
    # chart read, so a count made here is not lost when the tab is closed
    # and is not paid for twice. Only a run that produced something is
    # worth keeping - caching an empty result would suppress the retry
    # that might have worked.
    if result.accepted and candles:
        try:
            analysis_store.save(source, symbol, tf.value, candles[-1].open_time, len(candles), {
                "timeframe": tf.value, "accepted": result.accepted, "rejected": result.rejected,
                "projection": projection, "coverage": result.coverage,
                "summary": result.summary, "reasoning": result.reasoning,
                "steps_used": result.steps_used, "error": result.error,
            }, model=result.model)
        except SQLAlchemyError as exc:
            # A cache that cannot be written is a lost saving, not a lost
            # analysis: the answer below is already complete.
            logger.warning("could not cache analyst result for %s %s: %s", symbol, tf.value, exc)

    return {
        "symbol": symbol,
        "timeframe": tf.value,
        "model": result.model,
        "finished": result.finished,
        "note": result.note,
        "error": result.error,
        "summary": result.summary,
        "reasoning": result.reasoning,
        "steps_used": result.steps_used,
        # What this run consumed and, when the provider publishes a price
        # for this model, what it cost. Read from the catalogue, never
        # hardcoded - a price typed into this repo would be wrong the first
        # time the provider changed it, and wrong silently.
        "usage": run_cost(result.usage, resolve_api_key(api_key), result.model, base_url),
        "steps": [{"tool": call.name, "args": call.args, "result": call.result_summary}
                  for call in result.steps],
        # The conversation itself. "Why did it stop there" is unanswerable
        # from a list of tool names, so the model's own words and its
        # reasoning travel with the result.
        "transcript": result.transcript,
        # Where the count says price should go next, computed server-side
        # from waves already on the chart - the model names the wave, never
        # a price.
        # The same measured base rates the multi-timeframe view shows: the
        # share of THIS chart's past swings that carried at least that far.
        "projection": projection,
        "coverage": result.coverage,
        "accepted": result.accepted,
        "rejected": result.rejected,
        "waves": result.waves,
        # The clean chart this count belongs to. Returned with the answer so
        # the analyst tab draws the EXACT series the agent analysed - not a
        # separately-fetched one that could differ by a candle.
        "candles": [candle_to_dict(c) for c in candles],
    }


@app.post("/api/ai/multi")
def ai_multi_timeframe(api_key: str = Body("", embed=True),
                       source: str = Body("synthetic", embed=True),
                       symbol: str = Body("SYNTHETIC", embed=True),
                       limit: int = Body(1500, embed=True, ge=100, le=10000),
                       cycles: int = Body(2, embed=True, ge=1, le=10),
                       model: str = Body(DEFAULT_AI_MODEL, embed=True),
                       base_url: str = Body(DEFAULT_AI_API_BASE, embed=True),
                       timeout: float = Body(DEFAULT_READ_TIMEOUT, embed=True, gt=0, le=MAX_READ_TIMEOUT),
                       thinking: str = Body("on", embed=True),
                       max_steps: int = Body(DEFAULT_MAX_STEPS, embed=True, ge=1, le=MAX_MAX_STEPS),
                       force: bool = Body(False, embed=True)):
    """Count 5m, 15m, 1h and 4h, then reconcile them into one opinion.

    A count from a single timeframe answers a question nobody asked: a
    five-wave advance on 5m inside a 4h correction is a bounce, and only
    looking at both says so.

    Timeframes whose data has not moved are READ FROM CACHE rather than
    recounted. A full analyst run is a dozen model calls over a whole
    history, and a 4h chart produces one new candle every four hours - so
    the saving is large and the risk is nil, because freshness is decided
    by the newest candle the saved analysis saw, not by a clock. The
    response names which timeframes were reused and which were recomputed,
    so the saving is visible rather than claimed.

    The percentages attached to targets are MEASURED, not asked for: they
    are the share of this chart's own past swings that carried at least
    that far, with the sample size alongside. A model asked for a
    percentage returns a confident number with nothing behind it, and a
    percentage reads as measurement even when it is invention."""
    return run_multi_work(api_key, source, symbol, limit, cycles, model, base_url, timeout,
                          thinking, max_steps, force)


# --- background jobs -------------------------------------------------
# A multi-timeframe run was measured at 12m13s on the real deploy (see
# ai_advisor/jobs.py): the server finished and answered correctly, and the
# phone had dropped that connection minutes earlier, so the page showed
# "Load failed" AFTER the tokens were spent. These endpoints are the fix:
# the POST returns a job id in milliseconds, the work continues on its own
# thread, and the page collects the answer whenever it can.

@app.post("/api/ai/multi/start")
def ai_multi_start(api_key: str = Body("", embed=True),
                   source: str = Body("synthetic", embed=True),
                   symbol: str = Body("SYNTHETIC", embed=True),
                   limit: int = Body(1500, embed=True, ge=100, le=10000),
                   cycles: int = Body(2, embed=True, ge=1, le=10),
                   model: str = Body(DEFAULT_AI_MODEL, embed=True),
                   base_url: str = Body(DEFAULT_AI_API_BASE, embed=True),
                   timeout: float = Body(DEFAULT_READ_TIMEOUT, embed=True, gt=0, le=MAX_READ_TIMEOUT),
                   thinking: str = Body("on", embed=True),
                   max_steps: int = Body(DEFAULT_MAX_STEPS, embed=True, ge=1, le=MAX_MAX_STEPS),
                   force: bool = Body(False, embed=True)):
    """Start a multi-timeframe run and return its job id immediately."""
    label = f"{source}:{normalize_symbol(symbol) if source == 'bybit' else symbol}:mtf"
    existing = jobs.find_running("multi", label)
    if existing is not None:
        # Two taps, two tabs, or a retry after a dropped connection. Paying
        # for the same four analyst runs twice is the expensive mistake
        # here, so the second request attaches to the first.
        return {**existing.snapshot(include_result=False), "joined": True}

    def work(note):
        note(f"Starting {label}.")
        return run_multi_work(api_key, source, symbol, limit, cycles, model, base_url,
                              timeout, thinking, max_steps, force, on_progress=note)

    return {**jobs.start("multi", label, work).snapshot(), "joined": False}


@app.post("/api/ai/analyst/start")
def ai_analyst_start(api_key: str = Body("", embed=True),
                     source: str = Body("synthetic", embed=True),
                     symbol: str = Body("SYNTHETIC", embed=True),
                     timeframe: str = Body("5m", embed=True),
                     limit: int = Body(1500, embed=True, ge=100, le=10000),
                     cycles: int = Body(2, embed=True, ge=1, le=10),
                     model: str = Body(DEFAULT_AI_MODEL, embed=True),
                     base_url: str = Body(DEFAULT_AI_API_BASE, embed=True),
                     timeout: float = Body(DEFAULT_READ_TIMEOUT, embed=True, gt=0, le=MAX_READ_TIMEOUT),
                     reasoning_effort: str = Body(DEFAULT_REASONING_EFFORT, embed=True),
                     thinking: str = Body("on", embed=True),
                     max_steps: int = Body(DEFAULT_MAX_STEPS, embed=True, ge=1, le=MAX_MAX_STEPS)):
    """Start a single-timeframe analyst run and return its job id."""
    label = f"{source}:{normalize_symbol(symbol) if source == 'bybit' else symbol}:{timeframe}"
    existing = jobs.find_running("analyst", label)
    if existing is not None:
        return {**existing.snapshot(include_result=False), "joined": True}

    def work(note):
        note(f"Starting {label}.")
        return run_analyst_work(api_key, source, symbol, timeframe, limit, cycles, model,
                                base_url, timeout, reasoning_effort, thinking, max_steps,
                                on_progress=note)

    return {**jobs.start("analyst", label, work).snapshot(), "joined": False}


@app.get("/api/ai/job")
def ai_job(job_id: str = Query(...), since: int = Query(0, ge=0)):
    """Where a run has got to, and its result once there is one.

    `since` is how many progress lines the caller already has, so polling
    a long run does not re-send the whole transcript every few seconds."""
    job = jobs.get(job_id)
    if job is None:
        # Deliberately a 404 with an explanation: a job id that outlived a
        # restart is a real thing that happens, and "unknown job" with the
        # reason is more use than an empty result that reads as "finished
        # with nothing".
        raise HTTPException(404, "Unknown job - it finished more than an hour ago, or the "
                                 "server restarted while it was running. Any timeframe that "
                                 "completed is still saved and will be reused.")
    snapshot = job.snapshot()
    snapshot["progress"] = snapshot["progress"][since:]
    snapshot["progress_total"] = len(job.progress)
    return snapshot


# The source under which a count made OUTSIDE the analyst loop is stored.
# Kept apart from "bybit" on purpose: two readings of the same instrument
# must be comparable side by side, not overwriting each other.
CLAUDE_SOURCE = "claude"


# Which timeframes a full pass covers, shortest first. Four degrees is
# what makes a count checkable against itself: a 4h wave 3 that the 1h
# chart cannot subdivide into five is a 4h wave 3 worth doubting.
CLAUDE_TIMEFRAMES = ("5m", "15m", "1h", "4h")

# 1500 bars per timeframe. Not a round number for its own sake: Bybit
# serves 1000 rows per request, so this is two pages, and 1500 4h bars is
# eight months - long enough that a cycle-degree count has something to
# count, which a 200-bar window does not.
CLAUDE_DEFAULT_LIMIT = 1500


def build_claude_timeframe(symbol: str, timeframe: str, limit: int,
                           on_progress=None) -> Dict:
    """Fetch a full history for one degree, file it, and count it.

    No model is called anywhere in here, so this costs nothing but a
    couple of Bybit requests - which is the point. The expensive analyst
    run is for a second opinion on a chart; getting the chart itself
    labelled end to end is arithmetic and should not be billed."""
    def note(line: str) -> None:
        if on_progress:
            on_progress(line)

    candles, resolved, degree = load_candles("bybit", symbol, timeframe, limit, 2)
    note(f"{degree.value}: fetched {len(candles)} candles from Bybit")
    # File them first. A count is only re-checkable against the exact bars
    # it was made on, and those bars are gone from the exchange's window
    # by tomorrow.
    stored = candle_store.save(resolved, degree.value, candles)
    note(f"{degree.value}: filed {stored} candles")

    count = deep_count.build_count(candles, degree, resolved)
    note(f"{degree.value}: deviation {count.get('deviation_pct')}% -> "
         f"{len(count.get('accepted') or [])} structures, "
         f"{len(count.get('subwaves') or [])} subwaves, "
         f"{round(100 * (count.get('coverage') or {}).get('covered_fraction', 0))}% labelled")

    # Base rates measured at the SAME deviation the count was made at, so
    # "past swings that reached this far" means swings of the degree the
    # count is about, not of some other one.
    count["projection"] = annotate_projection(count.get("projection"), candles,
                                              float(count.get("deviation_pct") or 1.0))
    count["summary"] = claude_summary(count)

    # A reading written about this chart survives the recount. The count
    # itself is derived - rerun it and you get it back - but the prose
    # beside it is not, and a warm-up on every restart would quietly erase
    # it. Same rule as the labelled history: a rebuild must not be a way
    # of losing something that cannot be rebuilt.
    previous = analysis_store.load(CLAUDE_SOURCE, resolved, degree.value)
    carried = (previous.payload or {}).get("reading") if previous else ""
    if carried and not count.get("reading"):
        count["reading"] = carried

    if candles:
        try:
            analysis_store.save(CLAUDE_SOURCE, resolved, degree.value,
                                candles[-1].open_time, len(candles), count,
                                model="engine-rules")
        except SQLAlchemyError as exc:
            logger.warning("could not store claude count for %s %s: %s",
                           resolved, degree.value, exc)
    return count


def claude_summary(count: Dict) -> str:
    """One factual paragraph about what was found. Every number in it comes
    from the count itself - nothing here is a view."""
    accepted = count.get("accepted") or []
    if not accepted:
        return (f"No structure on {count.get('candles_analysed', 0)} {count.get('timeframe', '')} "
                "bars survived the hard rules at any deviation tried.")
    kinds: Dict[str, int] = {}
    for structure in accepted:
        kinds[structure.get("structure", "?")] = kinds.get(structure.get("structure", "?"), 0) + 1
    newest = accepted[-1]
    labels = "-".join(str(w.get("label")) for w in (newest.get("waves") or []))
    covered = round(100 * (count.get("coverage") or {}).get("covered_fraction", 0))
    parts = [
        f"{count.get('candles_analysed', 0)} {count.get('timeframe', '')} bars, "
        f"{covered}% of the span labelled at {count.get('deviation_pct')}% deviation.",
        ", ".join(f"{n}x {kind}" for kind, n in sorted(kinds.items())) + ".",
        f"The newest structure is a {newest.get('structure')} counted {labels}"
        + (" and still forming." if newest.get("partial") else " and complete."),
    ]
    projection = count.get("projection") or {}
    if projection.get("next_label"):
        parts.append(f"Wave {projection['next_label']} is the one now expected, "
                     f"{projection.get('basis', '')}, primary target "
                     f"{projection.get('primary_target')}.")
    if count.get("invalidation") is not None:
        parts.append(f"The count fails at {round(float(count['invalidation']), 6):g}.")
    return " ".join(parts)


@app.post("/api/claude/build")
def claude_build(symbol: str = Body("INJUSDT", embed=True),
                 timeframes: Optional[List[str]] = Body(None, embed=True),
                 limit: int = Body(CLAUDE_DEFAULT_LIMIT, embed=True, ge=200, le=5000)):
    """Fetch and count a full history on every timeframe. Free.

    Runs as a background job because four Bybit fetches plus four counts
    take longer than a browser is willing to hold a request open - the
    same reason the analyst runs were moved off the request thread."""
    symbol = normalize_symbol(symbol)
    wanted = [t for t in (timeframes or CLAUDE_TIMEFRAMES) if t in TIMEFRAME_ORDER]
    if not wanted:
        raise HTTPException(400, f"No usable timeframes; expected some of {', '.join(TIMEFRAME_ORDER)}")
    wanted.sort(key=lambda t: TIMEFRAME_ORDER[t])

    existing = jobs.find_running("claude-build", symbol)
    if existing is not None:
        return {**existing.snapshot(), "joined": True}

    def work(note):
        results, errors = [], []
        for timeframe in wanted:
            try:
                count = build_claude_timeframe(symbol, timeframe, limit, on_progress=note)
                results.append({"timeframe": timeframe,
                                "candles": count.get("candles_analysed", 0),
                                "structures": len(count.get("accepted") or []),
                                "subwaves": len(count.get("subwaves") or []),
                                "coverage": count.get("coverage", {}),
                                "summary": count.get("summary", "")})
            except HTTPException as exc:
                # One timeframe failing must not lose the three that
                # worked - each is saved as it finishes.
                note(f"{timeframe}: {exc.detail}")
                errors.append({"timeframe": timeframe, "error": str(exc.detail)})
            except Exception as exc:            # noqa: BLE001
                note(f"{timeframe}: {exc}")
                errors.append({"timeframe": timeframe, "error": str(exc)})
        return {"symbol": symbol, "timeframes": results, "errors": errors}

    return {**jobs.start("claude-build", symbol, work).snapshot(), "joined": False}


# Symbols whose full count is rebuilt when the process starts, comma
# separated (T3_WARMUP_SYMBOLS=INJUSDT,BTCUSDT). Left unset, nothing
# happens at startup at all.
#
# Why this exists: a deploy replaces the container, and the first person to
# open the tab after one should not be the one who has to notice it is
# empty and press a button. It is free - Bybit klines cost nothing and no
# model is called - so the only thing to be careful about is doing it too
# OFTEN, which the freshness guard below handles.
WARMUP_SYMBOLS_ENV = "T3_WARMUP_SYMBOLS"

# A count younger than this is left alone. Restarts happen in bursts
# (a deploy, a crash loop, a scale event) and refetching four timeframes
# on each one is pointless traffic.
WARMUP_MAX_AGE_SECONDS = 3600.0


def warmup_symbols() -> List[str]:
    raw = os.getenv(WARMUP_SYMBOLS_ENV, "")
    return [normalize_symbol(part) for part in raw.split(",") if part.strip()]


def stale_timeframes(symbol: str, now: Optional[float] = None) -> List[str]:
    """Which degrees have no count, or one old enough to be worth redoing."""
    now = time.time() if now is None else now
    fresh = set()
    try:
        for cached in analysis_store.list_for(CLAUDE_SOURCE, symbol):
            if now - (cached.created_at or 0) < WARMUP_MAX_AGE_SECONDS:
                fresh.add(cached.timeframe)
    except Exception:                       # noqa: BLE001 - a warm-up must
        return list(CLAUDE_TIMEFRAMES)      # never take the process down
    return [t for t in CLAUDE_TIMEFRAMES if t not in fresh]


@app.on_event("startup")
def start_lead_engine() -> None:
    """Bring the Market Lead Engine up, if it is switched on.

    Wrapped so that a Lead Engine that cannot start leaves a dashboard
    that runs without it - the isolation requirement, enforced here rather
    than assumed. `LeadEngine.start()` already swallows its own failures;
    this second guard covers the construction of the engine itself."""
    if not lead_engine_config.enabled():
        logger.info("lead engine: disabled (%s is not true)", lead_engine_config.ENABLED_ENV)
        return
    try:
        started = get_lead_engine().start()
        logger.info("lead engine: start() returned %s", started)
    except Exception:                       # noqa: BLE001 - see docstring
        logger.exception("lead engine failed to start; the dashboard is unaffected")


@app.on_event("shutdown")
def stop_lead_engine() -> None:
    if not lead_engine_config.enabled():
        return
    try:
        get_lead_engine().stop()
    except Exception:                       # noqa: BLE001
        logger.exception("lead engine did not stop cleanly")


@app.on_event("startup")
def warm_up_counts() -> None:
    for symbol in warmup_symbols():
        wanted = stale_timeframes(symbol)
        if not wanted:
            logger.info("warm-up: %s is already counted and fresh", symbol)
            continue
        if jobs.find_running("claude-build", symbol) is not None:
            continue

        def work(note, symbol=symbol, wanted=wanted):
            done, failed = [], []
            for timeframe in wanted:
                try:
                    count = build_claude_timeframe(symbol, timeframe,
                                                   CLAUDE_DEFAULT_LIMIT, on_progress=note)
                    done.append({"timeframe": timeframe,
                                 "candles": count.get("candles_analysed", 0),
                                 "structures": len(count.get("accepted") or []),
                                 "subwaves": len(count.get("subwaves") or []),
                                 "coverage": count.get("coverage", {}),
                                 "summary": count.get("summary", "")})
                except Exception as exc:    # noqa: BLE001
                    note(f"{timeframe}: {exc}")
                    failed.append({"timeframe": timeframe, "error": str(exc)})
            return {"symbol": symbol, "timeframes": done, "errors": failed}

        logger.info("warm-up: counting %s on %s", symbol, ", ".join(wanted))
        jobs.start("claude-build", symbol, work)


@app.get("/api/claude/chart")
def claude_chart(symbol: str = Query("INJUSDT"), timeframe: str = Query("4h")):
    """The chart and the count for the second-opinion tab.

    The candles come from whatever series was last fetched and filed
    (database/candle_store.py), aggregated up to the requested timeframe
    the way an exchange builds a coarser bar out of finer ones. That
    matters for honesty as much as convenience: the count in this tab was
    made on exactly these bars, so it is drawn on exactly these bars
    rather than on a freshly fetched window that has since moved.

    A timeframe FINER than what is stored cannot be invented - 5m does not
    divide out of 15m - and says so rather than returning something
    plausible."""
    symbol = normalize_symbol(symbol)
    try:
        degree = Timeframe(timeframe)
    except ValueError:
        raise HTTPException(400, f"Unsupported timeframe {timeframe}")

    stored: List = []
    base: Optional[Timeframe] = None
    for candidate in (degree, Timeframe.M15, Timeframe.M5, Timeframe.M1):
        rows = candle_store.load(symbol, candidate.value)
        if rows:
            stored, base = rows, candidate
            break

    note = ""
    if not stored:
        note = (f"No candles stored for {symbol} yet. Press Build to fetch "
                f"{CLAUDE_DEFAULT_LIMIT} bars of every timeframe from Bybit and count them - "
                "no model is called, so it costs nothing.")
        candles = []
    elif base == degree:
        candles = stored
    elif base.seconds < degree.seconds:
        candles = aggregate_candles(stored, degree)
        note = f"{degree.value} bars aggregated from the stored {base.value} series."
    else:
        candles = []
        note = (f"Only {base.value} candles are stored for {symbol}, and {degree.value} is finer - "
                f"a {degree.value} bar cannot be divided out of a {base.value} one. Press Build to "
                f"fetch {degree.value} directly.")

    cached = analysis_store.load(CLAUDE_SOURCE, symbol, degree.value)
    payload = cached.payload if cached else {}
    if not payload and candles:
        note = (note + " " if note else "") + \
            f"No count stored for {degree.value} yet - press Build."
    return {
        "symbol": symbol,
        "timeframe": degree.value,
        "candles": [candle_to_dict(c) for c in candles],
        "note": note,
        "analysed_at": cached.created_at if cached else None,
        "model": cached.model if cached else "",
        "accepted": payload.get("accepted", []),
        "rejected": payload.get("rejected", []),
        # The swing skeleton the count was built on, so the chart can show
        # what was labelled AND what was there to label.
        "pivots": payload.get("pivots", []),
        # i-ii-iii-iv-v inside each wave 1, 3 and 5 - the detail whose
        # absence made the first version of this tab useless.
        "subwaves": payload.get("subwaves", []),
        "projection": payload.get("projection"),
        "invalidation": payload.get("invalidation"),
        "correction_zone": payload.get("correction_zone"),
        "deviation_pct": payload.get("deviation_pct"),
        "candles_analysed": payload.get("candles_analysed"),
        "coverage": payload.get("coverage", {}),
        "summary": payload.get("summary", ""),
        "reading": payload.get("reading", ""),
        "reasoning": payload.get("reasoning", ""),
    }


@app.get("/api/claude/timeframes")
def claude_timeframes(symbol: str = Query("INJUSDT")):
    """Which timeframes this tab has a count for, shortest first."""
    symbol = normalize_symbol(symbol)
    rows = []
    for cached in analysis_store.list_for(CLAUDE_SOURCE, symbol):
        if cached.timeframe == MTF_CACHE_KEY:
            continue
        payload = cached.payload
        projection = payload.get("projection") or {}
        rows.append({
            "timeframe": cached.timeframe,
            "analysed_at": cached.created_at,
            "candles": cached.candle_count,
            "structures": len(payload.get("accepted") or []),
            "subwaves": len(payload.get("subwaves") or []),
            "coverage": payload.get("coverage", {}),
            "next_label": projection.get("next_label"),
            "primary_target": projection.get("primary_target"),
            "invalidation": payload.get("invalidation"),
            "summary": payload.get("summary", ""),
        })
    rows.sort(key=lambda r: TIMEFRAME_ORDER.get(r["timeframe"], 99))
    return {"symbol": symbol, "known_timeframes": list(CLAUDE_TIMEFRAMES),
            "default_limit": CLAUDE_DEFAULT_LIMIT, "timeframes": rows}


@app.get("/api/ai/saved")
def ai_saved(source: str = Query("synthetic"), symbol: str = Query("SYNTHETIC")):
    """Every timeframe ever analysed for this instrument, newest first.

    The analyst tab used to show exactly one count: whatever was just run.
    A multi-timeframe pass computes four and they vanished from that tab
    the moment it was reopened, even though all four were saved. This is
    the list that makes the tab ACCUMULATE - a new run on 4h adds to what
    is there rather than replacing it, and a count paid for yesterday is
    still one click away."""
    resolved = normalize_symbol(symbol) if source == "bybit" else symbol
    out = []
    for cached in analysis_store.list_for(source, resolved):
        if cached.timeframe == MTF_CACHE_KEY:
            continue                # the verdict, not a timeframe - /api/ai/multi/saved serves it
        payload = cached.payload
        out.append({
            "timeframe": cached.timeframe,
            "analysed_at": cached.created_at,
            "last_candle_time": cached.last_candle_time,
            "candles": cached.candle_count,
            "model": cached.model,
            "structures": len(payload.get("accepted") or []),
            "coverage": payload.get("coverage", {}),
            "summary": payload.get("summary", ""),
            "accepted": payload.get("accepted", []),
            "projection": payload.get("projection"),
        })
    out.sort(key=lambda row: TIMEFRAME_ORDER.get(row["timeframe"], 99))
    return {"symbol": resolved, "source": source, "timeframes": out}


@app.get("/api/ai/multi/saved")
def ai_multi_saved(source: str = Query("synthetic"), symbol: str = Query("SYNTHETIC")):
    """The last multi-timeframe verdict for this instrument, if any.

    This is what makes a dropped connection cost nothing: the run that
    finished while the page was gone is still here to be shown, together
    with how far behind the charts have moved since."""
    resolved = normalize_symbol(symbol) if source == "bybit" else symbol
    cached = analysis_store.load(source, resolved, MTF_CACHE_KEY)
    if cached is None:
        return {"saved": None}
    return {"saved": cached.payload, "analysed_at": cached.created_at,
            "last_candle_time": cached.last_candle_time, "model": cached.model}


@app.post("/api/ai/multi/clear")
def ai_multi_clear(source: str = Body("synthetic", embed=True),
                   symbol: str = Body("SYNTHETIC", embed=True)):
    """Forget every saved analysis for one instrument - the escape hatch
    for "recompute regardless of what the cache thinks"."""
    removed = analysis_store.clear(source, normalize_symbol(symbol) if source == "bybit" else symbol)
    return {"cleared": removed}


@app.post("/api/ai/diagnose")
def ai_diagnose_endpoint(api_key: str = Body("", embed=True),
                         model: str = Body(DEFAULT_AI_MODEL, embed=True),
                         base_url: str = Body(DEFAULT_AI_API_BASE, embed=True),
                         timeout: float = Body(45.0, embed=True, gt=0, le=MAX_READ_TIMEOUT),
                         probe: bool = Body(True, embed=True)):
    """Two checks, in the order that actually narrows the problem.

    First a catalogue listing (`GET {base}/models`): it needs the key and
    the same base URL, and runs NO inference. If that answers, the key, the
    endpoint and the network are all fine by construction, and everything
    slow afterwards belongs to the model or the queue in front of it.

    Then a raw look at the chat endpoint: status line, response headers,
    whether the body is really an event stream, the first bytes, and how
    long each phase took. Neither half raises on a timeout - a timeout is
    the observation, and the partial result is the evidence.

    This exists because every other error message in this module is a
    sentence written from an assumption about the cause, and "the model did
    not answer in 180s" is the same sentence whether the cause is a slow
    model, a queued request, a gateway that buffers, or a typo in a URL."""
    key = resolve_api_key(api_key)
    report: Dict[str, object] = {"model": model, "base_url": base_url}
    try:
        report["access"] = ai_check_access(key, base_url=base_url, timeout=min(timeout, 30.0),
                                           model=model)
    except AIAdvisorError as exc:
        report["access"] = {"ok": False, "error": str(exc)}
    # The chat probe runs either way: when access is fine it measures the
    # model, and when access is broken its raw status corroborates why.
    if probe:
        report["chat"] = ai_diagnose(key, model=model, base_url=base_url, timeout=timeout)
    # And the part that names the cause rather than describing the symptom:
    # the same trivial prompt under several configurations, each differing
    # from the last by exactly one thing.
    if key and probe:
        report["probe"] = ai_probe_variants(key, model=model, base_url=base_url,
                                            per_variant_timeout=min(timeout, 25.0))
    return report


@app.post("/api/ai/analyst")
def ai_analyst(api_key: str = Body("", embed=True), source: str = Body("synthetic", embed=True),
               symbol: str = Body("SYNTHETIC", embed=True), timeframe: str = Body("5m", embed=True),
               limit: int = Body(1500, embed=True, ge=100, le=10000),
               cycles: int = Body(2, embed=True, ge=1, le=10),
               model: str = Body(DEFAULT_AI_MODEL, embed=True),
               base_url: str = Body(DEFAULT_AI_API_BASE, embed=True),
               timeout: float = Body(DEFAULT_READ_TIMEOUT, embed=True, gt=0, le=MAX_READ_TIMEOUT),
               reasoning_effort: str = Body(DEFAULT_REASONING_EFFORT, embed=True),
               thinking: str = Body("on", embed=True),
               max_steps: int = Body(DEFAULT_MAX_STEPS, embed=True, ge=1, le=MAX_MAX_STEPS)):
    """The AI analyst: label a CLEAN chart from scratch, as an agent.

    Everything else in this app shows the deterministic engine's own count,
    with the AI at most offering a second opinion on top of it. This
    endpoint deliberately does the opposite: it loads the candles and hands
    the model nothing else - no pivots, no BOS/CHoCH, no scenarios, no
    Fibonacci - and lets it work the chart with the tools in
    ai_advisor/analyst_tools.py until it submits a count.

    That isolation is the point. A model shown an existing markup tends to
    agree with it, which makes it useless as an independent read; a model
    shown only price has to actually find the structure. Which is also why
    the frontend gives this its own tab with its own bare chart: the two
    counts must be comparable, not superimposed.

    The answer is not trusted any more than before. Every structure the
    agent submits is re-validated against the same hard Elliott rules
    (elliott_engine/external_count.py) before it is returned, and anything
    that breaks one comes back in `rejected` with the rule it broke rather
    than being quietly dropped or quietly drawn."""
    try:
        return run_analyst_work(api_key, source, symbol, timeframe, limit, cycles, model,
                                base_url, timeout, reasoning_effort, thinking, max_steps)
    except ToolError as exc:
        raise HTTPException(422, str(exc))
    except AIAdvisorError as exc:
        raise HTTPException(502, str(exc))


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
