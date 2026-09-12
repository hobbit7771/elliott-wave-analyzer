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

import os

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="T3 Elliott Wave Trading Engine Dashboard")


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


app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
