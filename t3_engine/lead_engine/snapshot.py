"""One compact, machine-readable view of everything, for an AI client.

The brief's main endpoint. An agent asked to "analyse INJUSDT now" should
get MTF candles, order flow, order book, open interest, liquidations, SMC,
Elliott, EMA, Fibonacci, BTC lead and the active signal in ONE call, with
stable field names and no prose.

Three rules shape the shape:

  COMPACT       no HTML, no long text, no nesting for its own sake. A
                field is a number or a short stable string.
  DATED         every block carries how old its data is. A snapshot that
                presents a nine-second-old book as current is worse than
                one that admits it, because the client cannot tell.
  HONEST        `signals_valid` and `quality` are computed from the health
                gate, not from whether the numbers look plausible. Stale
                data comes back marked stale.

Assembled here rather than in the route so the MCP adapter and the REST
route return the identical structure - one shape, one place.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from t3_engine.lead_engine.candles_rest import (
    INTERVAL_SECONDS,
    ema_series,
    fetch_candles,
    normalize_interval,
)

# The timeframes a multi-timeframe snapshot covers, coarsest first: an
# analyst reads the big picture before the detail, and so does an agent.
MTF_TIMEFRAMES = ("4h", "1h", "15m", "5m", "1m")

# How many candles each timeframe carries in an MTF snapshot. Enough for a
# 200-period EMA to exist, not so many that one call returns a megabyte.
MTF_CANDLES = 260

SCHEMA_VERSION = "1.0.0"


def _age_ms(stamp_ms: Optional[int], now_ms: int) -> Optional[float]:
    if not stamp_ms:
        return None
    return max(0.0, float(now_ms - int(stamp_ms)))


def _valued(value: Any, source: str, age_ms: Optional[float] = None) -> Dict[str, Any]:
    """A value with its provenance, for the fields where it matters.

    Used sparingly: wrapping every number would triple the payload for no
    gain. It is applied where a client could reasonably act on a figure
    that turns out to be minutes old."""
    return {"value": value, "source": source, "age_ms": age_ms}


def market_snapshot(engine, symbol: str) -> Dict[str, Any]:
    """Everything about one instrument, in one object."""
    symbol = symbol.upper()
    frame = engine.get_state(symbol, force=True)
    now_ms = int(time.time() * 1000)
    if not frame.get("tracked"):
        return {"schema_version": SCHEMA_VERSION, "symbol": symbol, "tracked": False,
                "server_time": now_ms, "quality": "unavailable",
                "signals_valid": False,
                "detail": frame.get("detail", "not subscribed")}

    health = frame.get("health") or {}
    pressure = frame.get("pressure") or {}
    layers = frame.get("layers") or {}
    prebreak = frame.get("prebreak") or {}
    book = frame.get("orderbook") or {}
    flow = frame.get("trade_flow") or {}
    cvd = frame.get("cvd") or {}
    oi = frame.get("open_interest") or {}
    liquidations = frame.get("liquidations") or {}
    smc = frame.get("smc") or {}

    signals_valid = bool(health.get("signals_enabled"))
    # Three answers, not two. "stale" is the case where the socket is up
    # and frames are arriving but what they carry is old - which a client
    # must be able to tell apart from a feed that is merely imperfect.
    status = health.get("status")
    if status == "OK":
        quality = "ok"
    elif status in ("WS_CONNECTED_DATA_STALE", "STALE_DATA"):
        quality = "stale"
    else:
        quality = "degraded"

    windows = flow.get("windows") or {}

    return {
        "schema_version": SCHEMA_VERSION,
        "symbol": symbol,
        "tracked": True,
        "server_time": now_ms,
        "exchange_time": frame.get("health", {}).get("last_book_ms") or None,
        "price": frame.get("price"),
        "mark_price": (frame.get("ticker") or {}).get("markPrice"),

        # --- the headline numbers, flat and stable ---
        "long_pressure": pressure.get("long_pressure"),
        "short_pressure": pressure.get("short_pressure"),
        "pressure_confidence": pressure.get("confidence"),
        "conflict": (pressure.get("conflict_detail") or {}).get("level"),
        "conflict_note": (pressure.get("conflict_detail") or {}).get("note"),

        "break_score_long": (prebreak.get("long") or {}).get("break_score"),
        "break_score_short": (prebreak.get("short") or {}).get("break_score"),
        # Whether either of those may be read as a probability, and why
        # not when it may not. See calibration.py.
        "calibration": {
            "long": (prebreak.get("long") or {}).get("calibration"),
            "short": (prebreak.get("short") or {}).get("calibration"),
        },

        # --- the five layers ---
        "structure": layers.get("structure"),
        "flow": layers.get("flow"),
        "book": layers.get("book"),
        "derivatives": layers.get("derivatives"),
        "btc_lead": layers.get("btc_lead"),

        # --- microstructure, flat field names an agent can rely on ---
        "microstructure": {
            "best_bid": book.get("best_bid"), "best_ask": book.get("best_ask"),
            "spread": book.get("spread"), "spread_bps": book.get("spread_bps"),
            "microprice": book.get("microprice"),
            "obi_1": (book.get("obi") or {}).get("obi1"),
            "obi_5": (book.get("obi") or {}).get("obi5"),
            "obi_10": (book.get("obi") or {}).get("obi10"),
            "obi_25": (book.get("obi") or {}).get("obi25"),
            "obi_50": (book.get("obi") or {}).get("obi50"),
            "weighted_obi": book.get("weighted_obi"),
            "book_alignment": (book.get("alignment") or {}).get("book_alignment"),
            "book_alignment_label": book.get("alignment_label"),
            "bid_pulling": book.get("bid_pulling"), "ask_pulling": book.get("ask_pulling"),
            "bid_replenishment": book.get("bid_replenishment"),
            "ask_replenishment": book.get("ask_replenishment"),
            "walls": frame.get("walls"),
            "cvd": cvd.get("cvd"),
            "cvd_5s": (cvd.get("cvd_change") or {}).get("15s"),
            "cvd_60s": (cvd.get("cvd_change") or {}).get("60s"),
            "cvd_divergence": cvd.get("divergence"),
            "normalized_delta_5s": (layers.get("flow") or {}).get(
                "detail", {}).get("normalized_delta_5s"),
            "normalized_delta_60s": (layers.get("flow") or {}).get(
                "detail", {}).get("normalized_delta_60s"),
            "delta_5s": (windows.get("5s") or {}).get("delta"),
            "trade_velocity": flow.get("trades_per_sec"),
            "velocity_zscore": flow.get("velocity_zscore"),
            "velocity_state": flow.get("velocity_state"),
        },
        "open_interest": {
            "value": oi.get("open_interest"), "delta": oi.get("oi_delta"),
            "delta_pct": oi.get("oi_delta_pct"), "trend": oi.get("oi_trend"),
            "interpretation": oi.get("interpretation"),
            "age_ms": _age_ms(oi.get("updated_at_ms"), now_ms),
        },
        "liquidations": {
            "state": liquidations.get("state"),
            "velocity": liquidations.get("velocity"),
            "peak_velocity_60s": liquidations.get("peak_velocity_60s"),
            "windows": liquidations.get("windows"),
        },
        "smc": smc,
        "elliott": frame.get("elliott"),
        "support_levels": _levels_from(prebreak, "short"),
        "resistance_levels": _levels_from(prebreak, "long"),
        "active_signal": frame.get("signal"),

        # --- freshness, on every response ---
        "health": health,
        "data_age_ms": health.get("book_age_ms"),
        "book_age_ms": health.get("book_age_ms"),
        "trade_age_ms": health.get("trade_age_ms"),
        "oi_age_ms": health.get("oi_age_ms"),
        "ws_latency_ms": health.get("ws_latency_ms"),
        "engine_status": health.get("status"),
        "quality": quality,
        "signals_valid": signals_valid,
    }


def _levels_from(prebreak: Dict[str, Any], side: str) -> List[Dict[str, Any]]:
    """The level under stress on this side, or an entry that says why
    there is none.

    An empty list is not an answer - it cannot be told apart from a bug,
    and "no resistance identified below visible swings" was exactly that
    ambiguity shipped as a message."""
    kind = "support" if side == "short" else "resistance"
    block = prebreak.get(side) or {}
    if block.get("level"):
        return [{"price": block["level"], "tests": block.get("tests"),
                 "break_score": block.get("break_score"), "kind": kind}]
    diagnosis = prebreak.get("levels") or {}
    note = diagnosis.get(f"{kind}_note") or ""
    if not note:
        return []
    return [{"price": None, "kind": kind, "available": False, "reason": note,
             "closed_bars": diagnosis.get("closed_bars"),
             "swings_found": diagnosis.get("swings_found"),
             "levels_known": diagnosis.get("levels")}]


def timeframe_context(symbol: str, timeframe: str, base_url: str,
                      candles: Optional[List[Dict[str, Any]]] = None,
                      limit: int = MTF_CANDLES) -> Dict[str, Any]:
    """One timeframe's shape: OHLC summary, EMAs, swings, structure.

    Candles may be supplied (so a caller fetching several timeframes does
    not fetch twice) or fetched here."""
    label = normalize_interval(timeframe) or "5m"
    rows = candles if candles is not None else fetch_candles(symbol, label, limit, base_url)
    if not rows:
        return {"timeframe": label, "available": False,
                "detail": "no candles returned by the exchange"}

    closes = [c["close"] for c in rows]
    highs = [c["high"] for c in rows]
    lows = [c["low"] for c in rows]
    emas = ema_series(rows)
    last = rows[-1]

    # Structure from this engine's own routine, over closed candles only.
    from t3_engine.lead_engine.smc_engine import Candle as SmcCandle, SmcEngine

    engine = SmcEngine(symbol, label)
    for row in rows:
        engine.update(SmcCandle(start_ms=row["time"] * 1000, open=row["open"],
                                high=row["high"], low=row["low"], close=row["close"],
                                volume=row.get("volume", 0.0), closed=row.get("closed", True)))
    structure = engine.state().as_dict()

    return {
        "timeframe": label,
        "available": True,
        "candles": len(rows),
        "interval_seconds": INTERVAL_SECONDS.get(label),
        "current_candle": {"time": last["time"], "open": last["open"], "high": last["high"],
                           "low": last["low"], "close": last["close"],
                           "state": "LIVE" if not last.get("closed", True) else "CLOSED"},
        "ohlc_summary": {
            "first_time": rows[0]["time"], "last_time": last["time"],
            "high": max(highs), "low": min(lows),
            "close": closes[-1],
            "change_pct": round((closes[-1] - closes[0]) / closes[0] * 100.0, 4)
            if closes[0] else None,
        },
        "ema": {name: (line[-1]["value"] if line else None) for name, line in emas.items()},
        "structure": structure,
        # Hoisted alongside the rest of the flat names an agent can rely
        # on without walking into the nested block.
        "trend": structure.get("trend"),
        "premium_discount": structure.get("premium_discount"),
        "range_position": structure.get("range_position"),
        "swing_high": structure.get("swing_high"),
        "swing_low": structure.get("swing_low"),
        "bos": structure.get("bos"), "bos_direction": structure.get("bos_direction"),
        "choch": structure.get("choch"), "choch_direction": structure.get("choch_direction"),
        "support": structure.get("swing_low"),
        "resistance": structure.get("swing_high"),
    }


def multi_timeframe_snapshot(engine, symbol: str, base_url: str,
                             timeframes=MTF_TIMEFRAMES) -> Dict[str, Any]:
    """The brief's `get_multi_tf_snapshot`: every timeframe plus the live
    microstructure, in one object."""
    symbol = symbol.upper()
    frames = {}
    for label in timeframes:
        try:
            frames[label] = timeframe_context(symbol, label, base_url)
        except Exception as exc:                 # noqa: BLE001 - one bad
            frames[label] = {"timeframe": label, "available": False,   # timeframe must
                             "detail": f"{type(exc).__name__}: {exc}"}  # not lose the rest
    market = market_snapshot(engine, symbol)
    return {
        "schema_version": SCHEMA_VERSION,
        "symbol": symbol,
        "server_time": int(time.time() * 1000),
        "timeframes": frames,
        "microstructure": market.get("microstructure"),
        "long_pressure": market.get("long_pressure"),
        "short_pressure": market.get("short_pressure"),
        "layers": {name: market.get(name) for name in
                   ("structure", "flow", "book", "derivatives", "btc_lead")},
        "open_interest": market.get("open_interest"),
        "liquidations": market.get("liquidations"),
        "active_signal": market.get("active_signal"),
        "health": market.get("health"),
        "quality": market.get("quality"),
        "signals_valid": market.get("signals_valid"),
    }
