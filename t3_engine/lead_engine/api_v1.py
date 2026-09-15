"""The versioned, authenticated, read-only external interface.

`/api/v1/lead-engine/*`, for another agent or an AI client to read this
engine programmatically. Versioned from the start so a later change to the
internal shape cannot break an integration.

Separate from `api.py`, which serves the dashboard's own tab. The split is
deliberate: the tab is same-origin and unauthenticated by design, the
external surface is token-gated and rate-limited, and mixing them would
mean one set of rules had to be loosened to fit the other.

READ ONLY, structurally. There is no write path in this package to
expose - it cannot place an order, close one, change leverage, touch an
exchange credential or reach the analyser's execution engine, and
`test_lead_engine_isolation.py` asserts the imports that would be needed
do not exist. Every route here is a GET.

This adapter is not the engine. If it is disabled, misconfigured or
throws, the Lead Engine keeps ingesting Bybit and keeps scoring; only the
external view goes away. That ordering - Bybit → engine → internal state →
API → client - is the whole architecture, and nothing downstream of the
engine is allowed to become a dependency of it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from t3_engine.lead_engine import auth
from t3_engine.lead_engine import config as config_module
from t3_engine.lead_engine import fibonacci as fib_module
from t3_engine.lead_engine import snapshot as snapshot_module
from t3_engine.lead_engine.candles_rest import (
    DEFAULT_LIMIT,
    INTERVALS,
    MAX_LIMIT,
    ema_series,
    fetch_candles,
    normalize_interval,
)
from t3_engine.lead_engine.engine import get_engine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/lead-engine", tags=["lead-engine-external"])

# One drawing store for the process. The browser keeps its own copy in
# localStorage as well; see fibonacci.DrawingStore for why both.
_drawings = fib_module.DrawingStore()


def drawings() -> fib_module.DrawingStore:
    return _drawings


def _guard(request: Request) -> Optional[JSONResponse]:
    """The check every route makes. Returns a response to send, or None."""
    result = auth.authorize(request.headers)
    if result.ok:
        return None
    return JSONResponse(result.as_error(), status_code=result.status)


def _envelope(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Freshness on every response, without exception.

    The brief is explicit and it is the right rule: a client must never
    have to guess whether what it just received is current."""
    engine = get_engine()
    return {
        **payload,
        "server_time": int(time.time() * 1000),
        "engine_running": engine.running(),
        "read_only": True,
        "api_version": "v1",
    }


@router.get("/health")
def health(request: Request):
    """Feed freshness for every symbol. The cheapest call, and the one an
    agent should make before trusting anything else."""
    blocked = _guard(request)
    if blocked:
        return blocked
    engine = get_engine()
    rows = []
    for symbol in engine.symbols():
        state = engine.states.get(symbol)
        if state is None:
            continue
        frame = state.snapshot(btc_state=engine._btc_state())
        rows.append({"symbol": symbol, **(frame.get("health") or {})})
    return _envelope({"symbols": rows,
                      "stream": engine.stream.stats.as_dict() if engine.stream else None})


@router.get("/status")
def status(request: Request):
    blocked = _guard(request)
    if blocked:
        return blocked
    return _envelope(get_engine().status())


@router.get("/symbols")
def symbols(request: Request):
    blocked = _guard(request)
    if blocked:
        return blocked
    engine = get_engine()
    return _envelope({"symbols": engine.symbols(),
                      "btc_reference": config_module.BTC_SYMBOL,
                      "timeframes": sorted(INTERVALS)})


@router.get("/snapshot/{symbol}")
def snapshot(request: Request, symbol: str):
    """THE endpoint for an AI client: everything current, one call."""
    blocked = _guard(request)
    if blocked:
        return blocked
    return _envelope(snapshot_module.market_snapshot(get_engine(), symbol))


@router.get("/multi-tf/{symbol}")
def multi_tf(request: Request, symbol: str):
    """4h/1h/15m/5m/1m context plus the live microstructure."""
    blocked = _guard(request)
    if blocked:
        return blocked
    engine = get_engine()
    return _envelope(snapshot_module.multi_timeframe_snapshot(
        engine, symbol, engine.config.rest_base))


@router.get("/state/{symbol}")
def state(request: Request, symbol: str):
    blocked = _guard(request)
    if blocked:
        return blocked
    return _envelope(get_engine().get_state(symbol, force=True))


@router.get("/market/{symbol}")
def market(request: Request, symbol: str):
    blocked = _guard(request)
    if blocked:
        return blocked
    frame = get_engine().get_state(symbol)
    return _envelope({"symbol": symbol.upper(), "price": frame.get("price"),
                      "ticker": frame.get("ticker"), "health": frame.get("health")})


@router.get("/orderbook/{symbol}")
def orderbook(request: Request, symbol: str):
    blocked = _guard(request)
    if blocked:
        return blocked
    frame = get_engine().get_state(symbol)
    return _envelope({"symbol": symbol.upper(), "orderbook": frame.get("orderbook"),
                      "walls": frame.get("walls"), "microprice": frame.get("microprice"),
                      "book_layer": (frame.get("layers") or {}).get("book")})


@router.get("/flow/{symbol}")
def flow(request: Request, symbol: str):
    blocked = _guard(request)
    if blocked:
        return blocked
    frame = get_engine().get_state(symbol)
    return _envelope({"symbol": symbol.upper(), "trade_flow": frame.get("trade_flow"),
                      "cvd": frame.get("cvd"),
                      "flow_layer": (frame.get("layers") or {}).get("flow")})


@router.get("/derivatives/{symbol}")
def derivatives(request: Request, symbol: str):
    blocked = _guard(request)
    if blocked:
        return blocked
    frame = get_engine().get_state(symbol)
    return _envelope({"symbol": symbol.upper(),
                      "open_interest": frame.get("open_interest"),
                      "liquidations": frame.get("liquidations"),
                      "derivatives_layer": (frame.get("layers") or {}).get("derivatives")})


@router.get("/structure/{symbol}")
def structure(request: Request, symbol: str):
    blocked = _guard(request)
    if blocked:
        return blocked
    frame = get_engine().get_state(symbol)
    return _envelope({"symbol": symbol.upper(), "smc": frame.get("smc"),
                      "elliott": frame.get("elliott"),
                      "structure_layer": (frame.get("layers") or {}).get("structure"),
                      "prebreak": frame.get("prebreak")})


@router.get("/pressure/{symbol}")
def pressure(request: Request, symbol: str):
    blocked = _guard(request)
    if blocked:
        return blocked
    return _envelope(get_engine().get_pressure(symbol))


@router.get("/signals/{symbol}")
def signals(request: Request, symbol: str):
    blocked = _guard(request)
    if blocked:
        return blocked
    return _envelope(get_engine().get_signal(symbol))


@router.get("/history/{symbol}")
def history(request: Request, symbol: str, limit: int = Query(300, ge=1, le=2000)):
    blocked = _guard(request)
    if blocked:
        return blocked
    return _envelope(get_engine().get_history(symbol, limit))


@router.get("/candles/{symbol}")
def candles(request: Request, symbol: str,
            timeframe: str = Query("5m"),
            limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)):
    """Historical candles from Bybit REST, oldest first.

    The newest row carries `closed: false` - it is the bar the WebSocket
    is still updating. A client that appends it as a finished candle will
    be one bar wrong forever."""
    blocked = _guard(request)
    if blocked:
        return blocked
    label = normalize_interval(timeframe)
    if label is None:
        return JSONResponse({"error": f"unknown timeframe {timeframe!r}",
                             "supported": sorted(INTERVALS)}, status_code=400)
    engine = get_engine()
    rows = fetch_candles(symbol.upper(), label, limit, engine.config.rest_base)
    return _envelope({"symbol": symbol.upper(), "timeframe": label,
                      "candles": rows, "count": len(rows)})


@router.get("/indicators/{symbol}")
def indicators(request: Request, symbol: str,
               timeframe: str = Query("5m"),
               limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)):
    """EMA 9/18/50/200, computed server-side from the same closes.

    Offered as well as computed in the browser so the two can be compared;
    a test asserts they agree."""
    blocked = _guard(request)
    if blocked:
        return blocked
    label = normalize_interval(timeframe)
    if label is None:
        return JSONResponse({"error": f"unknown timeframe {timeframe!r}"}, status_code=400)
    engine = get_engine()
    rows = fetch_candles(symbol.upper(), label, limit, engine.config.rest_base)
    lines = ema_series(rows)
    return _envelope({
        "symbol": symbol.upper(), "timeframe": label,
        "ema": {name: (line[-1]["value"] if line else None) for name, line in lines.items()},
        "series": lines,
        "formula": "EMA_t = a*close_t + (1-a)*EMA_(t-1), a = 2/(period+1), seeded with an SMA",
    })


@router.get("/fibonacci/{symbol}")
def fibonacci(request: Request, symbol: str, timeframe: str = Query("5m")):
    """Saved Fibonacci drawings for this symbol AND timeframe.

    Keyed by both: a drawing made on the 5m chart must not appear on the
    1h one."""
    blocked = _guard(request)
    if blocked:
        return blocked
    label = normalize_interval(timeframe) or "5m"
    rows = [d.as_dict() for d in _drawings.list(symbol, label)]
    return _envelope({"symbol": symbol.upper(), "timeframe": label,
                      "drawings": rows, "count": len(rows),
                      "default_ratios": list(fib_module.ALL_LEVELS)})


@router.get("/stream")
async def stream(request: Request, symbol: str = Query(...),
                 interval_ms: int = Query(500, ge=200, le=5000)):
    """Server-Sent Events: compact DELTAS, not the whole state.

    The brief asks for change-only updates, and the reason is bandwidth on
    a phone: the full snapshot is tens of kilobytes and almost none of it
    changes between ticks. Each event carries only the fields whose value
    actually moved, so a quiet market produces almost no traffic.

    SSE rather than a WebSocket because this is one-directional by nature -
    the client subscribes and reads - and SSE reconnects itself, works
    through every proxy that passes HTTP, and needs no framing library."""
    blocked = _guard(request)
    if blocked:
        return blocked
    engine = get_engine()
    symbol = symbol.upper()

    def compact(frame: Dict[str, Any]) -> Dict[str, Any]:
        pressure_block = frame.get("pressure") or {}
        prebreak_block = frame.get("prebreak") or {}
        book = frame.get("orderbook") or {}
        health_block = frame.get("health") or {}
        return {
            "price": frame.get("price"),
            "long_pressure": pressure_block.get("long_pressure"),
            "short_pressure": pressure_block.get("short_pressure"),
            "confidence": pressure_block.get("confidence"),
            "conflict": (pressure_block.get("conflict_detail") or {}).get("level"),
            "break_long": (prebreak_block.get("long") or {}).get("break_score"),
            "break_short": (prebreak_block.get("short") or {}).get("break_score"),
            "signal": (frame.get("signal") or {}).get("state"),
            "signal_direction": (frame.get("signal") or {}).get("direction"),
            "obi_5": (book.get("obi") or {}).get("obi5"),
            "book_alignment": (book.get("alignment") or {}).get("book_alignment"),
            "cvd": (frame.get("cvd") or {}).get("cvd"),
            "status": health_block.get("status"),
            "signals_valid": health_block.get("signals_enabled"),
            "book_age_ms": health_block.get("book_age_ms"),
        }

    async def events():
        previous: Dict[str, Any] = {}
        yield f"event: hello\ndata: {json.dumps({'symbol': symbol, 'interval_ms': interval_ms})}\n\n"
        while True:
            if await request.is_disconnected():
                break
            try:
                frame = engine.get_state(symbol)
                current = compact(frame) if frame.get("tracked") else {
                    "status": "NOT_TRACKED"}
            except Exception as exc:             # noqa: BLE001 - a stream
                current = {"status": "ERROR", "detail": str(exc)}   # that dies is worse
            delta = {k: v for k, v in current.items() if previous.get(k) != v}
            previous = current
            if delta:
                delta["t"] = int(time.time() * 1000)
                yield f"event: update\ndata: {json.dumps(delta)}\n\n"
            else:
                # A comment keeps the connection and any proxy in front of
                # it alive without costing a parse on the client.
                yield ": keepalive\n\n"
            await asyncio.sleep(interval_ms / 1000.0)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.get("/schema")
def schema(request: Request):
    """What this API offers, for a client to discover at runtime."""
    blocked = _guard(request)
    if blocked:
        return blocked
    return _envelope({
        "schema_version": snapshot_module.SCHEMA_VERSION,
        "read_only": True,
        "endpoints": sorted(
            route.path for route in router.routes if hasattr(route, "path")),
        "timeframes": sorted(INTERVALS),
        "mtf_timeframes": list(snapshot_module.MTF_TIMEFRAMES),
        "auth": {"headers": ["Authorization: Bearer <token>", "x-api-key: <token>"],
                 "env": auth.API_KEY_ENV},
        "rate_limit": {"per_second": auth.RATE_LIMIT_PER_SECOND,
                       "burst": auth.BURST,
                       "burst_window_seconds": auth.BURST_WINDOW_SECONDS},
        "notes": [
            "Every response carries server_time and the relevant *_age_ms fields.",
            "signals_valid is false whenever the feed is stale; do not act on a "
            "snapshot with quality != 'ok'.",
            "break_score is a MODEL SCORE unless calibration.kind == 'PROBABILITY'.",
        ],
    })
