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
import os
import time
from typing import Dict

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
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
from t3_engine.market_data.rest_client import BinanceFuturesREST
from t3_engine.pipeline.live_loop import LiveTradingEngine

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="T3 Elliott Wave Trading Engine Dashboard")

# --- live engine registry (spec section 20: one running pipeline per
# symbol, driven by the public Binance WebSocket - no API key needed for
# market data). Kept as simple in-process state: this is a single-process
# dashboard, not a distributed deployment. ---
_live_engines: Dict[str, LiveTradingEngine] = {}
_live_tasks: Dict[str, asyncio.Task] = {}
_live_started_at: Dict[str, int] = {}
_live_errors: Dict[str, str] = {}


async def _run_live_guarded(symbol: str, engine: LiveTradingEngine) -> None:
    try:
        await engine.run_live_binance()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface it in /api/live/status instead of crashing the process
        _live_errors[symbol] = str(exc)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/run")
def run_backtest(source: str = Query("synthetic"), symbol: str = Query("SYNTHETIC"),
                  cycles: int = Query(2, ge=1, le=10), threshold: float = Query(60.0, ge=0, le=100),
                  limit: int = Query(1500, ge=100, le=1500)):
    if source == "binance":
        rest = BinanceFuturesREST()
        try:
            candles = rest.get_klines(symbol, Timeframe.M5, limit=limit)
        finally:
            rest.close()
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
            "Candles are a SYNTHETIC, clearly-labelled demo fixture (see backtest/synthetic_data.py) "
            "because this build environment cannot reach Binance. Pass source=binance&symbol=... "
            "to use real historical data when running somewhere with normal internet access."
        ) if source != "binance" else None,
    }


@app.post("/api/live/start")
async def start_live(symbol: str = Body(..., embed=True), equity: float = Body(10_000.0, embed=True),
                      threshold: float = Body(75.0, embed=True)):
    """Starts a live pipeline against Binance's PUBLIC WebSocket streams
    (aggTrade etc.) for `symbol` - no API key required, this is public
    market data, not account access. Runs in PAPER mode only."""
    symbol = symbol.upper()
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
    symbol = symbol.upper()
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
        }
        for symbol, engine in _live_engines.items()
    }


@app.get("/api/live/state")
def live_state(symbol: str = Query(...), timeframe: str = Query("5m")):
    symbol = symbol.upper()
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
    candles = engine.history[tf]
    return {
        "symbol": symbol,
        "timeframe": tf.value,
        "candles": [candle_to_dict(c) for c in candles],
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


@app.get("/")
def index():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@app.get("/sw.js")
def service_worker():
    # Served from the root path (not /static/sw.js) so its default scope
    # covers the whole app - a service worker registered from /static/
    # could only ever control /static/* requests.
    return FileResponse(os.path.join(_STATIC_DIR, "sw.js"), media_type="application/javascript")


app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
