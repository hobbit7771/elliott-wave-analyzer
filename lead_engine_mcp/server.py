"""MCP server over stdio.

Speaks the Model Context Protocol's JSON-RPC directly rather than through
the `mcp` SDK. That is a deliberate trade: the SDK is not installed in
this deployment and pulling it into the main image for one adapter would
be exactly the dependency creep the brief warns against, while the
protocol surface an adapter this simple needs - initialize, tools/list,
tools/call - is small enough to implement correctly and to test.

If the SDK is later added, `tools.py` is already the whole tool
definition; only this file would change.

Run it:

    LEAD_ENGINE_API_URL=https://your-host \\
    LEAD_ENGINE_API_KEY=<read-only token> \\
    python -m lead_engine_mcp.server

It reads one JSON-RPC message per line from stdin and writes one per line
to stdout. Nothing is ever written to stdout except protocol messages -
logging goes to stderr, because a stray print corrupts the stream and the
failure looks like a protocol bug rather than a logging one.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Dict, Optional

from lead_engine_mcp import SCHEMA_VERSION, __version__
from lead_engine_mcp.client import LeadEngineClient
from lead_engine_mcp.tools import call_tool, tool_list

# stderr, never stdout. See the module docstring.
logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(asctime)s %(levelname)s lead_engine_mcp: %(message)s")
logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"

SERVER_INFO = {"name": "lead-engine", "version": __version__}

INSTRUCTIONS = (
    "Read-only access to a live Bybit market-microstructure engine. "
    "Call get_health first: signals_valid is false whenever the feed is "
    "stale, and a snapshot with quality != 'ok' should not be acted on. "
    "get_market_snapshot gives everything current for one instrument in "
    "one call; get_multi_tf_snapshot adds 4h/1h/15m/5m/1m context. "
    "LONG_PRESSURE and break scores are MODEL SCORES, not probabilities, "
    "unless a response's calibration.kind says PROBABILITY. "
    "There are no trading tools here and none can be added: this adapter "
    "reads an API that has no write path."
)


def _result(request_id: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(message: Dict[str, Any],
           client: Optional[LeadEngineClient] = None) -> Optional[Dict[str, Any]]:
    """One JSON-RPC message in, one response out (or None for a notification).

    Pure: no I/O, so the whole protocol surface is testable without a
    process or a pipe."""
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        return _result(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        })

    if method in ("notifications/initialized", "initialized"):
        return None                     # a notification has no response

    if method == "ping":
        return _result(request_id, {})

    if method == "tools/list":
        return _result(request_id, {"tools": tool_list()})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        payload = call_tool(name, arguments, client)
        failed = isinstance(payload, dict) and "error" in payload
        return _result(request_id, {
            # Content as compact JSON text: an agent parses it, and a
            # human reading a transcript can still see what came back.
            "content": [{"type": "text",
                         "text": json.dumps(payload, separators=(",", ":"), default=str)}],
            "isError": bool(failed),
            "_meta": {"schema_version": SCHEMA_VERSION},
        })

    if request_id is None:
        return None                     # an unknown notification is ignored
    return _error(request_id, -32601, f"unknown method {method!r}")


def serve(stdin=None, stdout=None, client: Optional[LeadEngineClient] = None) -> None:
    """Read messages until stdin closes."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    client = client or LeadEngineClient()
    logger.info("serving MCP over stdio against %s", client.base_url)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            logger.warning("ignoring a line that is not JSON")
            continue
        try:
            response = handle(message, client)
        except Exception as exc:        # noqa: BLE001 - a tool that throws
            logger.exception("tool call failed")   # must not end the session
            response = _error(message.get("id"), -32603, f"{type(exc).__name__}: {exc}")
        if response is None:
            continue
        stdout.write(json.dumps(response, default=str) + "\n")
        stdout.flush()


if __name__ == "__main__":      # pragma: no cover - the entry point
    serve()
