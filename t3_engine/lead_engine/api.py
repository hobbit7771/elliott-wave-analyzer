"""HTTP surface for the Lead Engine, under /api/lead-engine.

Its own router, mounted by the dashboard with one `include_router` call.
Nothing here shares a path prefix, a handler or a response shape with the
older system's endpoints, so a change to either cannot alter the other's
contract.

Every route answers the same way when the engine is off: HTTP 200 with
`{"enabled": false, ...}` rather than a 404 or a 503. That is deliberate.
A 404 would be indistinguishable from a deploy that shipped without the
module, and the tab needs to tell the difference between "not built" and
"built, switched off" in order to say so.

The routes never touch the engine's internals - they call the same five
facade methods any other caller would (see engine.py). If a route needs
something the facade does not offer, the fix is a facade method.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from t3_engine.lead_engine import config as config_module
from t3_engine.lead_engine import storage as storage_module
from t3_engine.lead_engine import fibonacci as fib_module
from t3_engine.lead_engine.candles_rest import (
    DEFAULT_LIMIT,
    INTERVALS,
    MAX_LIMIT,
    ema_series,
    fetch_candles,
    normalize_interval,
)
from t3_engine.lead_engine.engine import LeadEngine, get_engine
from t3_engine.lead_engine.replay import run_replay

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/lead-engine", tags=["lead-engine"])

# One storage instance for the process, created lazily like the engine.
_storage: Optional[storage_module.Storage] = None


def storage() -> storage_module.Storage:
    global _storage
    if _storage is None:
        _storage = storage_module.Storage()
    return _storage


def engine() -> LeadEngine:
    return get_engine()


def _disabled() -> Dict[str, Any]:
    return {
        "enabled": False,
        "flag": config_module.ENABLED_ENV,
        "detail": ("The Market Lead Engine is switched off. Set "
                   f"{config_module.ENABLED_ENV}=true and redeploy to run it."),
    }


@router.get("/status")
def status() -> Dict[str, Any]:
    """Header data for the tab: stream, latency, and every symbol's state."""
    if not config_module.enabled():
        return {**_disabled(), "symbols": [], "stream": None}
    payload = engine().status()
    payload["storage"] = storage().stats()
    return payload


@router.get("/symbols")
def symbols() -> Dict[str, Any]:
    if not config_module.enabled():
        return {**_disabled(), "symbols": []}
    return {"enabled": True, "symbols": engine().symbols(),
            "btc_reference": config_module.BTC_SYMBOL,
            "kline_intervals": engine().config.kline_intervals}


@router.get("/state/{symbol}")
def state(symbol: str, force: bool = Query(False)) -> Dict[str, Any]:
    """Everything about one instrument, in one frame."""
    if not config_module.enabled():
        return _disabled()
    frame = engine().get_state(symbol, force=force)
    if frame.get("tracked"):
        # Filed as it is served, never on the stream thread - see
        # storage.Storage for why writes are buffered.
        try:
            storage().record_features(symbol.upper(), frame)
        except Exception:                        # noqa: BLE001
            logger.debug("lead_engine: feature row not stored", exc_info=True)
    return frame


@router.get("/signals/{symbol}")
def signals(symbol: str) -> Dict[str, Any]:
    if not config_module.enabled():
        return _disabled()
    return engine().get_signal(symbol)


@router.get("/metrics/{symbol}")
def metrics(symbol: str) -> Dict[str, Any]:
    if not config_module.enabled():
        return _disabled()
    return engine().get_metrics(symbol)


@router.get("/pressure/{symbol}")
def pressure(symbol: str) -> Dict[str, Any]:
    if not config_module.enabled():
        return _disabled()
    return engine().get_pressure(symbol)


@router.get("/history/{symbol}")
def history(symbol: str, limit: int = Query(300, ge=1, le=2000)) -> Dict[str, Any]:
    if not config_module.enabled():
        return {**_disabled(), "rows": []}
    return engine().get_history(symbol, limit)


@router.post("/subscribe")
def subscribe(symbol: str = Body(..., embed=True)) -> Dict[str, Any]:
    if not config_module.enabled():
        return _disabled()
    if not symbol or not symbol.strip():
        raise HTTPException(400, "symbol is required")
    return engine().subscribe(symbol.strip().upper())


@router.post("/replay/run")
def replay_run(symbol: str = Body(..., embed=True),
               messages: List[Dict[str, Any]] = Body(..., embed=True),
               break_pct: float = Body(0.004, embed=True, gt=0, le=0.5),
               horizon_minutes: int = Body(10, embed=True, ge=1, le=240),
               store: bool = Body(False, embed=True)) -> Dict[str, Any]:
    """Replay a recorded capture and score it.

    Runs on its OWN engine instance (see replay.py) - the live engine is
    never touched, so a replay cannot contaminate live state and a live
    stream cannot contaminate a backtest."""
    if not config_module.enabled():
        return _disabled()
    if not messages:
        raise HTTPException(400, "messages is empty; a replay needs recorded frames")
    if len(messages) > 200_000:
        raise HTTPException(413, f"{len(messages)} frames is more than one request should carry")
    report = run_replay(symbol.strip().upper(), messages,
                        config=engine().config,
                        horizon_ms=horizon_minutes * 60_000,
                        break_pct=break_pct)
    if store:
        try:
            storage().record_backtest({
                "created_at": report["created_at"], "symbol": report["symbol"],
                "events": report["events"], "precision_pct": report["precision_pct"],
                "recall_pct": report["recall_pct"],
                "false_positives": report["false_positives"],
                "median_lead_seconds": report["median_lead_seconds"],
                "mfe": report["mfe"], "mae": report["mae"],
                "expected_value": report["expected_value"],
                "detail": {"states_seen": report["states_seen"],
                           "call_count": report["call_count"]},
            })
        except Exception:                        # noqa: BLE001
            logger.warning("lead_engine: backtest row not stored", exc_info=True)
    return report


@router.get("/replay/schema")
def replay_schema() -> Dict[str, Any]:
    """What a capture has to look like to be replayable.

    Documented as an endpoint as well as in docs/lead-engine, because the
    shape is just Bybit's own frames and the easiest way to produce a
    valid capture is to record them verbatim."""
    return {
        "enabled": config_module.enabled(),
        "format": "a JSON array of raw Bybit v5 public WebSocket frames, verbatim",
        "required_fields": ["topic", "ts", "data"],
        "topics": ["orderbook.50.SYMBOL", "publicTrade.SYMBOL", "tickers.SYMBOL",
                   "allLiquidation.SYMBOL", "kline.{1,5,15,60,240}.SYMBOL"],
        "notes": [
            "Frames are sorted by exchange timestamp before replay, so a capture "
            "that interleaves topics out of order is still replayed in order.",
            "The order book needs its snapshot frame; without one every delta is "
            "refused and the book never syncs, which the report will show as "
            "DATA_FAILURE throughout.",
            "Outcomes are scored only from prices strictly after each signal.",
        ],
    }


@router.get("/schema.sql")
def schema_sql() -> Dict[str, Any]:
    """The SQL for this engine's own tables, for a one-time run."""
    return {"tables": list(storage_module.ALL_TABLES), "sql": storage_module.SCHEMA_SQL}


# ---- the chart's own data -------------------------------------------
# Same-origin and unauthenticated, like every other route in this file:
# these serve the dashboard's own workspace page. The token-gated
# equivalents for external clients live in api_v1.py.


@router.get("/candles/{symbol}")
def candles(symbol: str, timeframe: str = Query("5m"),
            limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)) -> Dict[str, Any]:
    """Historical candles, fetched ONCE per chart.

    The newest row carries `closed: false` - it is the bar the WebSocket
    keeps updating. The workspace calls this once per (symbol, timeframe)
    and then only ever calls `update()` on that one bar; re-fetching the
    series on a tick is the thing this shape exists to make unnecessary."""
    if not config_module.enabled():
        return {**_disabled(), "candles": []}
    label = normalize_interval(timeframe)
    if label is None:
        raise HTTPException(400, f"unknown timeframe {timeframe!r}; "
                                 f"supported: {', '.join(sorted(INTERVALS))}")
    rows = fetch_candles(symbol.upper(), label, limit, engine().config.rest_base)
    return {"enabled": True, "symbol": symbol.upper(), "timeframe": label,
            "candles": rows, "count": len(rows),
            "detail": "" if rows else "the exchange returned no candles for this pair"}


@router.get("/indicators/{symbol}")
def indicators(symbol: str, timeframe: str = Query("5m"),
               limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)) -> Dict[str, Any]:
    """EMA 9/18/50/200 computed server-side from the same closes the chart
    draws, so the browser's own calculation can be checked against it."""
    if not config_module.enabled():
        return _disabled()
    label = normalize_interval(timeframe)
    if label is None:
        raise HTTPException(400, f"unknown timeframe {timeframe!r}")
    rows = fetch_candles(symbol.upper(), label, limit, engine().config.rest_base)
    return {"enabled": True, "symbol": symbol.upper(), "timeframe": label,
            "series": ema_series(rows), "candles": len(rows)}


@router.get("/fibonacci/{symbol}")
def fibonacci(symbol: str, timeframe: str = Query("5m")) -> Dict[str, Any]:
    if not config_module.enabled():
        return {**_disabled(), "drawings": []}
    label = normalize_interval(timeframe) or "5m"
    from t3_engine.lead_engine.api_v1 import drawings as drawing_store

    return {"enabled": True, "symbol": symbol.upper(), "timeframe": label,
            "drawings": [d.as_dict() for d in drawing_store().list(symbol, label)],
            "default_ratios": list(fib_module.ALL_LEVELS)}


@router.post("/fibonacci/{symbol}")
def save_fibonacci(symbol: str,
                   timeframe: str = Body("5m", embed=True),
                   start_time: int = Body(..., embed=True),
                   start_price: float = Body(..., embed=True),
                   end_time: int = Body(..., embed=True),
                   end_price: float = Body(..., embed=True),
                   ratios: Optional[List[float]] = Body(None, embed=True)) -> Dict[str, Any]:
    """Save a drawing server-side as well as in the browser.

    Both, because they fail differently: the browser copy survives a
    redeploy, the server copy survives moving to another device."""
    if not config_module.enabled():
        return _disabled()
    label = normalize_interval(timeframe) or "5m"
    from t3_engine.lead_engine.api_v1 import drawings as drawing_store

    drawing = drawing_store().add(symbol, label, start_time, start_price,
                                  end_time, end_price, ratios)
    return {"enabled": True, "drawing": drawing.as_dict()}


@router.delete("/fibonacci/{symbol}")
def clear_fibonacci(symbol: str, timeframe: str = Query("5m"),
                    drawing_id: Optional[str] = Query(None)) -> Dict[str, Any]:
    if not config_module.enabled():
        return _disabled()
    label = normalize_interval(timeframe) or "5m"
    from t3_engine.lead_engine.api_v1 import drawings as drawing_store

    if drawing_id:
        return {"enabled": True, "removed": drawing_store().remove(symbol, label, drawing_id)}
    return {"enabled": True, "removed": drawing_store().reset(symbol, label)}
