"""Dashboard backend (spec section 18/28).

Serves real OHLC candles plus Elliott/Fibonacci/BOS-CHoCH/entry-SL-TP
overlays for the frontend (static/index.html, TradingView lightweight-
charts) to draw directly on real candlesticks - never schematic
placeholder lines, per section 18's explicit requirement.

By default `/api/run` replays the SYNTHETIC fixture (backtest/synthetic_
data.py) because this sandboxed build session cannot reach Binance (see
market_data/rest_client.py). Pass `?source=binance&symbol=BTCUSDT` to
pull real historical klines instead - that code path is real, it just
needs to run somewhere with outbound network access to actually work.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Dict, Optional

import httpx
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from t3_engine.ai_advisor.advisor import AIAdvisorError, request_commentary
from t3_engine.backtest.engine import BacktestConfig, BacktestEngine
from t3_engine.backtest.metrics import compute_metrics, compute_metrics_by_wave
from t3_engine.backtest.synthetic_data import generate_synthetic_series
from t3_engine.common.types import Timeframe
from t3_engine.dashboard.serialization import (
    candle_to_dict,
    position_to_dict,
    scenario_to_dict,
    signal_to_dict,
    structure_event_to_dict,
)
from t3_engine.market_data.bybit_rest_client import BybitAPIError, BybitFuturesREST
from t3_engine.market_data.fallback_symbols import FALLBACK_USDT_PERPETUAL_SYMBOLS
from t3_engine.market_data.rest_client import BinanceFuturesREST
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
# symbol, driven by the public Binance WebSocket - no API key needed for
# market data). Kept as simple in-process state: this is a single-process
# dashboard, not a distributed deployment. ---
_live_engines: Dict[str, LiveTradingEngine] = {}
_live_tasks: Dict[str, asyncio.Task] = {}
_live_started_at: Dict[str, int] = {}
_live_errors: Dict[str, str] = {}


_SYMBOL_STRIP_RE = re.compile(r"[^A-Za-z0-9]")


def normalize_symbol(raw: str) -> str:
    """Binance symbols are plain alphanumeric strings (e.g. `BTCUSDT`) -
    no slash, no space, no lowercase-vs-uppercase distinction. Users
    naturally type things like "BTC/USDT", "btc usdt" or paste a
    lowercase ticker; reject silently-wrong requests instead of sending
    garbage straight to Binance (or, worse, to a half-typed partial
    string on every keystroke - see the frontend's debounce/commit-on-
    start fix for the other half of this)."""
    return _SYMBOL_STRIP_RE.sub("", raw).upper()


def binance_error_message(exc: httpx.HTTPStatusError) -> str:
    status = exc.response.status_code
    if status == 451:
        return (
            "Binance returned HTTP 451 (Unavailable For Legal Reasons) - it blocks API access "
            "from this server's IP/region entirely, regardless of symbol or request. This is a "
            "regional restriction on Binance's side, not a bug: it commonly affects servers hosted "
            "in the US (e.g. Render's default 'oregon' region). Redeploying this service in a "
            "non-US region (e.g. Render's 'frankfurt' or 'singapore') is the known workaround."
        )
    if status == 418:
        return (
            "Binance returned HTTP 418 ('I'm a teapot') - this is Binance's documented response "
            "for an IP that has been temporarily auto-banned for exceeding its request rate limit. "
            "On shared hosting (Render's free/shared plans included), this IP address can be shared "
            "with other tenants, so a ban can be inherited from traffic you never sent. Requests are "
            "now paused for a cooldown instead of retrying immediately - repeating requests during a "
            "ban is what turns a short ban into a much longer one, per Binance's own rate-limit rules. "
            "If this persists, a Render plan with a dedicated/static outbound IP avoids inheriting "
            "other tenants' bans."
        )
    if status == 429:
        return "Binance returned HTTP 429 (rate limited) - requests are paused for a cooldown before retrying."
    return f"Binance API returned HTTP {status}: {exc.response.text[:300]}"


# --- shared Binance backoff: 418/429 responses mean STOP calling Binance
# for a while, from ANY endpoint - continuing to hit it during a ban is
# exactly what Binance's docs say escalates a short ban into a long one.
# This is process-wide (not per-endpoint) since the ban is IP-wide. ---
_binance_backoff_until: float = 0.0
_binance_backoff_message: Optional[str] = None


def _parse_retry_after(resp: httpx.Response) -> float:
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return 120.0  # Binance's shortest documented ban window
    try:
        return max(float(raw), 1.0)
    except ValueError:
        return 120.0  # Retry-After was an HTTP-date, not a delta - fall back


def _register_binance_failure(exc: httpx.HTTPStatusError) -> None:
    global _binance_backoff_until, _binance_backoff_message
    if exc.response.status_code in (418, 429):
        _binance_backoff_until = time.time() + _parse_retry_after(exc.response)
        _binance_backoff_message = binance_error_message(exc)


def _check_binance_backoff() -> None:
    remaining = _binance_backoff_until - time.time()
    if remaining > 0:
        raise HTTPException(429, f"{_binance_backoff_message} ({remaining:.0f}s remaining in cooldown)")


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
BUILD_VERSION = "BUILD-CHECK-002"


@app.get("/api/health")
def health():
    return {"status": "ok", "build": BUILD_VERSION}


# --- symbol list cache: Binance lists ~400 perpetual futures symbols and
# that list barely changes minute to minute, so we cache it in-process
# instead of hitting exchangeInfo on every dashboard page load. ---
_symbols_cache: Dict[str, object] = {"symbols": None, "fetched_at": 0.0, "source": "live"}
_SYMBOLS_CACHE_TTL_SECONDS = 3600.0


@app.get("/api/symbols")
def list_symbols():
    now = time.time()
    if _symbols_cache["symbols"] is not None and (now - _symbols_cache["fetched_at"]) < _SYMBOLS_CACHE_TTL_SECONDS:
        return {"symbols": _symbols_cache["symbols"], "cached": True, "source": _symbols_cache["source"]}

    binance_reason: Optional[str] = None
    if _binance_backoff_until > now:
        binance_reason = f"{_binance_backoff_message} ({_binance_backoff_until - now:.0f}s remaining in cooldown)"
    else:
        rest = BinanceFuturesREST()
        try:
            symbols = rest.list_symbols()
            _symbols_cache["symbols"] = symbols
            _symbols_cache["fetched_at"] = now
            _symbols_cache["source"] = "live"
            return {"symbols": symbols, "cached": False, "source": "live"}
        except httpx.HTTPStatusError as exc:
            _register_binance_failure(exc)
            binance_reason = binance_error_message(exc)
        except httpx.RequestError as exc:
            binance_reason = f"Could not reach Binance: {exc}"
        finally:
            rest.close()

    # Binance is unavailable - Bybit is a different exchange on different
    # infrastructure, so a Binance-side regional block or IP ban has no
    # bearing on whether it's reachable. Try it before giving up to the
    # static list, so the picker still gets a real, current symbol list
    # during a Binance outage instead of the hand-picked fallback.
    bybit = BybitFuturesREST()
    try:
        symbols = bybit.list_symbols()
        _symbols_cache["symbols"] = symbols
        _symbols_cache["fetched_at"] = now
        _symbols_cache["source"] = "bybit"
        return {"symbols": symbols, "cached": False, "source": "bybit",
                "reason": f"Binance unavailable ({binance_reason}) - serving Bybit's live list instead"}
    except (httpx.HTTPStatusError, httpx.RequestError, BybitAPIError):
        pass
    finally:
        bybit.close()

    return {"symbols": FALLBACK_USDT_PERPETUAL_SYMBOLS, "cached": False, "source": "fallback",
            "reason": binance_reason}


@app.get("/api/run")
def run_backtest(source: str = Query("synthetic"), symbol: str = Query("SYNTHETIC"),
                  cycles: int = Query(2, ge=1, le=10), threshold: float = Query(60.0, ge=0, le=100),
                  limit: int = Query(1500, ge=100, le=1500)):
    data_source = source
    fallback_note = None
    if source == "binance":
        symbol = normalize_symbol(symbol)
        if not symbol:
            raise HTTPException(400, "Symbol is empty after normalization - expected something like BTCUSDT")

        now = time.time()
        candles = None
        binance_status = None
        binance_message = None
        if _binance_backoff_until > now:
            binance_status = 429
            binance_message = (f"{_binance_backoff_message} "
                                f"({_binance_backoff_until - now:.0f}s remaining in cooldown)")
        else:
            rest = BinanceFuturesREST()
            try:
                candles = rest.get_klines(symbol, Timeframe.M5, limit=limit)
            except httpx.HTTPStatusError as exc:
                _register_binance_failure(exc)
                binance_status = exc.response.status_code
                binance_message = binance_error_message(exc)
            except httpx.RequestError as exc:
                binance_status = 502
                binance_message = f"Could not reach Binance: {exc}"
            finally:
                rest.close()

        if candles is None:
            # Binance unavailable - Bybit runs on separate infrastructure,
            # so a Binance-side ban/regional block doesn't affect it. Try
            # it before surfacing an error, so analysis keeps working on
            # real market data through a Binance outage.
            bybit = BybitFuturesREST()
            try:
                candles = bybit.get_klines(symbol, Timeframe.M5, limit=limit)
                data_source = "bybit"
                fallback_note = f"Binance unavailable ({binance_message}) - served from Bybit instead."
            except (httpx.HTTPStatusError, httpx.RequestError, BybitAPIError, ValueError):
                raise HTTPException(binance_status, binance_message)
            finally:
                bybit.close()
    else:
        candles = generate_synthetic_series(num_cycles=cycles)
        symbol = symbol if symbol != "SYNTHETIC" else "SYNTHETIC-DEMO"

    engine = BacktestEngine(BacktestConfig(symbol=symbol, entry_confidence_threshold=threshold))
    result = engine.run(candles)

    metrics = compute_metrics(result["closed_positions"])
    metrics_by_wave = compute_metrics_by_wave(result["closed_positions"])

    return {
        "symbol": symbol,
        "source": source,
        "data_source": data_source,
        "candles": [candle_to_dict(c) for c in candles],
        "scenarios": [scenario_to_dict(s) for s in engine.scenario_engine.scenarios],
        "structure_events": [structure_event_to_dict(e) for e in engine.structure.events],
        "signals": [signal_to_dict(s) for s in result["signals"]],
        "closed_positions": [position_to_dict(p) for p in result["closed_positions"]],
        "open_positions": [position_to_dict(p) for p in result["open_positions"]],
        "final_equity": result["final_equity"],
        "metrics": {
            "overall": dataclass_metrics_to_dict(metrics),
            "by_wave": {k: dataclass_metrics_to_dict(v) for k, v in metrics_by_wave.items()},
        },
        "note": (
            fallback_note if source == "binance" else
            "Candles are a SYNTHETIC, clearly-labelled demo fixture (see backtest/synthetic_data.py) "
            "because this build environment cannot reach Binance. Pass source=binance&symbol=... "
            "to use real historical data when running somewhere with normal internet access."
        ),
    }


@app.post("/api/live/start")
async def start_live(symbol: str = Body(..., embed=True), equity: float = Body(10_000.0, embed=True),
                      threshold: float = Body(75.0, embed=True)):
    """Starts a live pipeline against Binance's PUBLIC WebSocket streams
    (aggTrade etc.) for `symbol` - no API key required, this is public
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
            # Which exchange is actually feeding this session - run_live()
            # falls back from Binance to Bybit if Binance produces no trade
            # within its watchdog window (see pipeline/live_loop.py).
            "live_source": engine.live_source,
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
