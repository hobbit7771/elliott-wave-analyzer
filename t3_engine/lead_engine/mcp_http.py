"""MCP over HTTP, so a hosted AI can connect at all.

`lead_engine_mcp` speaks MCP over stdio, which is how Claude Desktop and
Claude Code launch a server: as a subprocess on the same machine. ChatGPT
cannot do that. A hosted assistant has no subprocess to launch and no
filesystem to launch it from - it can only reach a URL. So the same
protocol is offered over HTTP here.

This is a TRANSPORT, not a second implementation. Every message is handed
to `lead_engine_mcp.server.handle`, the same pure function the stdio
server uses, so the two cannot answer differently. What arrives is
JSON-RPC; what comes back is JSON-RPC.

The direction of the dependency is worth stating, because the whole
architecture rests on it:

    Bybit -> Lead Engine -> internal state -> REST API -> MCP -> AI

This module is on the far right. It imports the adapter; the adapter
imports nothing from `t3_engine` at all (asserted by a test). The adapter
reaches the engine the same way any external client does - over
`/api/v1/lead-engine`, through the same token and the same rate limit.
That costs a loopback request per tool call and is worth it: there is
exactly one path to the data, so there is exactly one set of rules to get
it wrong in.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from t3_engine.lead_engine import auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/lead-engine", tags=["lead-engine-mcp"])

# Where the adapter's loopback request goes. Render sets PORT; locally
# uvicorn's default is 8000.
DEFAULT_PORT = 8000


def _base_url() -> str:
    explicit = os.getenv("LEAD_ENGINE_API_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    port = os.getenv("PORT", "").strip() or str(DEFAULT_PORT)
    return f"http://127.0.0.1:{port}"


def _client():
    from lead_engine_mcp.client import LeadEngineClient

    return LeadEngineClient(base_url=_base_url(), api_key=auth.configured_key())


@router.post("/mcp")
async def mcp_endpoint(request: Request):
    """One JSON-RPC message in, one out.

    Auth is the same as every other external route - the namespace is off
    without EXTERNAL_AI_ACCESS_ENABLED, and every request carries the
    read-only token. An AI connecting here gets exactly the access a curl
    would: sixteen reads and no write path to reach."""
    blocked = _guard(request)
    if blocked:
        return blocked

    try:
        message = await request.json()
    except Exception:                            # noqa: BLE001
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32700, "message": "parse error: body is not JSON"}},
            status_code=400)

    from lead_engine_mcp.server import handle

    try:
        reply = handle(message, _client())
    except Exception as exc:                     # noqa: BLE001 - a tool that
        logger.exception("mcp over http: handling failed")   # throws must not
        return JSONResponse(                     # take the endpoint down
            {"jsonrpc": "2.0", "id": (message or {}).get("id"),
             "error": {"code": -32603,
                       "message": f"{type(exc).__name__}: {exc}"}},
            status_code=500)

    if reply is None:
        # A notification. JSON-RPC says answer nothing; HTTP needs a
        # status, and 202 is the one that means "taken, no reply".
        return JSONResponse(None, status_code=202)
    return JSONResponse(reply)


@router.get("/mcp")
def mcp_hello(request: Request):
    """What this endpoint is, for a human who pasted the URL into a
    browser to see whether it exists."""
    blocked = _guard(request)
    if blocked:
        return blocked
    from lead_engine_mcp.server import PROTOCOL_VERSION, SERVER_INFO
    from lead_engine_mcp.tools import tool_list

    return {
        "transport": "http",
        "protocol": "jsonrpc-2.0",
        "mcp_protocol_version": PROTOCOL_VERSION,
        "server": SERVER_INFO,
        "read_only": True,
        "methods": ["initialize", "tools/list", "tools/call", "ping"],
        "tools": [tool["name"] for tool in tool_list()],
        "how": "POST a JSON-RPC message to this same URL.",
    }


@router.get("/connect")
def connect(request: Request):
    """How to point an AI at this engine.

    Deliberately readable WITHOUT the token, because the first thing a
    person needs to know is whether the deployment is configured at all,
    and that question should not itself require a credential. It returns
    no market data and never returns the key."""
    public = str(request.base_url).rstrip("/")
    return describe(public)


def _guard(request: Request) -> Optional[JSONResponse]:
    result = auth.authorize(request.headers)
    if result.ok:
        return None
    return JSONResponse(result.as_error(), status_code=result.status)


def describe(public_url: str = "") -> Dict[str, Any]:
    """Everything a person needs to connect an AI, and nothing they
    shouldn't have.

    The token is never included - not masked, not partially shown, not
    at all. Whether one is CONFIGURED is a different question from what
    it is, and only the first is answered here."""
    base = (public_url or _base_url()).rstrip("/")
    return {
        "external_access_enabled": auth.external_enabled(),
        "api_key_configured": bool(auth.configured_key()),
        "api_key_env": auth.API_KEY_ENV,
        "flag_env": auth.EXTERNAL_ENV,
        "rest_base": f"{base}/api/v1/lead-engine",
        "mcp_http_url": f"{base}/api/v1/lead-engine/mcp",
        "snapshot_example": f"{base}/api/v1/lead-engine/snapshot/INJUSDT",
        "stream_example": f"{base}/api/v1/lead-engine/stream?symbol=INJUSDT",
        "rate_limit": {"per_second": auth.RATE_LIMIT_PER_SECOND,
                       "burst": auth.BURST,
                       "burst_window_seconds": auth.BURST_WINDOW_SECONDS},
    }
