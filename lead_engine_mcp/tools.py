"""The MCP tool surface: sixteen read-only tools over the engine's API.

Schemas are declared as data rather than built from decorators, for two
reasons. The transport below can then serve them without the MCP SDK
being installed - this deployment does not have it, and an adapter that
cannot start without an optional dependency is an adapter that does not
run. And a test can assert the exact tool list and shapes, which is what
keeps `docs/lead-engine/MCP.md` from drifting away from the code.

Every tool maps to one GET. No tool computes anything, and there is no
write tool - not disabled, not gated, ABSENT. See the package docstring.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from lead_engine_mcp import SCHEMA_VERSION
from lead_engine_mcp.client import LeadEngineClient

SYMBOL = {"type": "string",
          "description": "Instrument, e.g. INJUSDT. Case-insensitive."}
TIMEFRAME = {"type": "string",
             "description": "1m, 3m, 5m, 15m, 30m, 1h, 4h or 1d.",
             "default": "5m"}
LIMIT = {"type": "integer", "description": "How many rows.", "default": 300,
         "minimum": 1, "maximum": 1000}


def _schema(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": properties,
            "required": required or [], "additionalProperties": False}


# name -> (description, input schema, how to call the API)
TOOLS: Dict[str, Dict[str, Any]] = {
    "get_lead_engine_status": {
        "description": "Whether the engine is running, its stream statistics, "
                       "and every symbol's headline state. Cheap; call it first.",
        "schema": _schema({}),
        "call": lambda c, a: c.get("status"),
    },
    "get_symbols": {
        "description": "The instruments the engine is subscribed to, and the "
                       "timeframes it can serve candles for.",
        "schema": _schema({}),
        "call": lambda c, a: c.get("symbols"),
    },
    "get_market_snapshot": {
        "description": "EVERYTHING current for one symbol in one call: price, the "
                       "five layer scores, microstructure, open interest, "
                       "liquidations, SMC, Elliott, levels, active signal, and "
                       "data freshness. The main tool for analysing an instrument.",
        "schema": _schema({"symbol": SYMBOL}, ["symbol"]),
        "call": lambda c, a: c.get(f"snapshot/{a['symbol']}"),
    },
    "get_multi_tf_snapshot": {
        "description": "4h/1h/15m/5m/1m context in one call - OHLC summary, EMA "
                       "9/18/50/200, swing highs and lows, BOS/CHoCH, support and "
                       "resistance, and whether each timeframe's current candle is "
                       "LIVE or CLOSED - plus the live microstructure.",
        "schema": _schema({"symbol": SYMBOL}, ["symbol"]),
        "call": lambda c, a: c.get(f"multi-tf/{a['symbol']}"),
    },
    "get_pressure": {
        "description": "LONG_PRESSURE and SHORT_PRESSURE with the per-layer "
                       "breakdown, the confidence, and the conflict level. Note "
                       "these are MODEL SCORES, not probabilities.",
        "schema": _schema({"symbol": SYMBOL}, ["symbol"]),
        "call": lambda c, a: c.get(f"pressure/{a['symbol']}"),
    },
    "get_orderbook_state": {
        "description": "L2 book state: best bid/ask, spread, microprice, OBI at "
                       "1/5/10/25/50, weighted OBI, book alignment, pulling and "
                       "replenishment, and classified walls.",
        "schema": _schema({"symbol": SYMBOL}, ["symbol"]),
        "call": lambda c, a: c.get(f"orderbook/{a['symbol']}"),
    },
    "get_trade_flow": {
        "description": "Aggressive flow: normalised delta over eight windows, CVD "
                       "and its divergence against price, trade velocity and its "
                       "z-score, large prints.",
        "schema": _schema({"symbol": SYMBOL}, ["symbol"]),
        "call": lambda c, a: c.get(f"flow/{a['symbol']}"),
    },
    "get_derivatives_state": {
        "description": "Open interest with its four-way price/OI interpretation, "
                       "liquidation windows and flush state, funding when the "
                       "ticker carries it.",
        "schema": _schema({"symbol": SYMBOL}, ["symbol"]),
        "call": lambda c, a: c.get(f"derivatives/{a['symbol']}"),
    },
    "get_structure": {
        "description": "SMC state (HH/HL/LH/LL, BOS, CHoCH, sweep, FVG, order "
                       "block, premium/discount) and the pre-break reading with "
                       "all ten of its features.",
        "schema": _schema({"symbol": SYMBOL}, ["symbol"]),
        "call": lambda c, a: c.get(f"structure/{a['symbol']}"),
    },
    "get_elliott_state": {
        "description": "The Lead Engine's own wave CONTEXT - a deterministic "
                       "state machine, not the project's full wave analyser.",
        "schema": _schema({"symbol": SYMBOL}, ["symbol"]),
        "call": lambda c, a: _pick(c.get(f"structure/{a['symbol']}"), "elliott"),
    },
    "get_active_signal": {
        "description": "The current signal state, its direction, the level under "
                       "stress, and the break score.",
        "schema": _schema({"symbol": SYMBOL}, ["symbol"]),
        "call": lambda c, a: c.get(f"signals/{a['symbol']}"),
    },
    "get_recent_signals": {
        "description": "The engine's recent feature history: pressure, break "
                       "scores and signal state over time.",
        "schema": _schema({"symbol": SYMBOL, "limit": LIMIT}, ["symbol"]),
        "call": lambda c, a: c.get(f"history/{a['symbol']}",
                                   {"limit": a.get("limit", 300)}),
    },
    "get_candles": {
        "description": "Historical OHLCV from Bybit, oldest first. The newest row "
                       "has closed=false - it is the bar still forming.",
        "schema": _schema({"symbol": SYMBOL, "timeframe": TIMEFRAME, "limit": LIMIT},
                          ["symbol"]),
        "call": lambda c, a: c.get(f"candles/{a['symbol']}",
                                   {"timeframe": a.get("timeframe", "5m"),
                                    "limit": a.get("limit", 300)}),
    },
    "get_indicators": {
        "description": "EMA 9/18/50/200 for one symbol and timeframe, computed "
                       "from the same closes the chart draws.",
        "schema": _schema({"symbol": SYMBOL, "timeframe": TIMEFRAME, "limit": LIMIT},
                          ["symbol"]),
        "call": lambda c, a: c.get(f"indicators/{a['symbol']}",
                                   {"timeframe": a.get("timeframe", "5m"),
                                    "limit": a.get("limit", 300)}),
    },
    "get_fibonacci_levels": {
        "description": "Saved Fibonacci retracements for one symbol AND timeframe, "
                       "with every level priced.",
        "schema": _schema({"symbol": SYMBOL, "timeframe": TIMEFRAME}, ["symbol"]),
        "call": lambda c, a: c.get(f"fibonacci/{a['symbol']}",
                                   {"timeframe": a.get("timeframe", "5m")}),
    },
    "get_health": {
        "description": "Feed freshness for every symbol: WS latency, book age, "
                       "trade age, OI age, processing time, drops and reconnects. "
                       "Check this before acting on anything else - signals_valid "
                       "is false whenever the data is stale.",
        "schema": _schema({}),
        "call": lambda c, a: c.get("health"),
    },
}


def _pick(payload: Dict[str, Any], key: str) -> Dict[str, Any]:
    if not isinstance(payload, dict) or "error" in payload:
        return payload
    return {key: payload.get(key), "symbol": payload.get("symbol"),
            "server_time": payload.get("server_time")}


def tool_list() -> List[Dict[str, Any]]:
    """The MCP `tools/list` payload."""
    return [
        {"name": name,
         "description": spec["description"],
         "inputSchema": spec["schema"],
         # Advertised on every tool, so a client can see the guarantee
         # without reading the documentation.
         "annotations": {"readOnlyHint": True, "destructiveHint": False,
                         "idempotentHint": True, "openWorldHint": True},
         "_meta": {"schema_version": SCHEMA_VERSION}}
        for name, spec in sorted(TOOLS.items())
    ]


def call_tool(name: str, arguments: Optional[Dict[str, Any]],
              client: Optional[LeadEngineClient] = None) -> Dict[str, Any]:
    """Run one tool. Unknown names are an answer, not an exception."""
    spec = TOOLS.get(name)
    if spec is None:
        return {"error": f"unknown tool {name!r}",
                "available": sorted(TOOLS)}
    arguments = dict(arguments or {})
    for required in spec["schema"].get("required", []):
        if not arguments.get(required):
            return {"error": f"{name} needs {required!r}"}
    if "symbol" in arguments:
        arguments["symbol"] = str(arguments["symbol"]).strip().upper()
    handler: Callable = spec["call"]
    return handler(client or LeadEngineClient(), arguments)
