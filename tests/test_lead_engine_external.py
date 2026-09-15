"""The external surface: auth, the versioned API, the stream, and MCP.

The guarantee these tests exist to hold is that the adapter is NOT the
engine. Everything here can be switched off, misconfigured or broken and
the Lead Engine keeps ingesting Bybit and keeps scoring - the dependency
runs one way, Bybit -> engine -> state -> API -> MCP -> agent, and never
back.
"""

import asyncio
import io
import json

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

import t3_engine.dashboard.server as server_module
from lead_engine_mcp import server as mcp_server
from lead_engine_mcp.tools import TOOLS, call_tool, tool_list
from t3_engine.lead_engine import api_v1
from t3_engine.lead_engine import auth
from t3_engine.lead_engine import config as le_config
from t3_engine.lead_engine import engine as engine_module
from t3_engine.lead_engine import snapshot as snapshot_module

client = TestClient(server_module.app)

TOKEN = "read-only-test-token"

V1 = "/api/v1/lead-engine"
EXTERNAL_ROUTES = [
    f"{V1}/status", f"{V1}/symbols", f"{V1}/health", f"{V1}/schema",
    f"{V1}/snapshot/INJUSDT", f"{V1}/state/INJUSDT", f"{V1}/market/INJUSDT",
    f"{V1}/orderbook/INJUSDT", f"{V1}/flow/INJUSDT", f"{V1}/derivatives/INJUSDT",
    f"{V1}/structure/INJUSDT", f"{V1}/pressure/INJUSDT", f"{V1}/signals/INJUSDT",
    f"{V1}/history/INJUSDT", f"{V1}/fibonacci/INJUSDT",
]


@pytest.fixture(autouse=True)
def _clean():
    engine_module.reset_engine()
    auth.limiter().reset()
    yield
    engine_module.reset_engine()
    auth.limiter().reset()


@pytest.fixture
def external_on(monkeypatch):
    monkeypatch.setenv(auth.EXTERNAL_ENV, "true")
    monkeypatch.setenv(auth.API_KEY_ENV, TOKEN)
    monkeypatch.setenv(le_config.ENABLED_ENV, "true")


@pytest.fixture
def external_off(monkeypatch):
    monkeypatch.delenv(auth.EXTERNAL_ENV, raising=False)
    monkeypatch.delenv(auth.EXTERNAL_ENV_PREFIXED, raising=False)


# ---- the switch ---------------------------------------------------------

def test_external_access_is_off_by_default(external_off):
    """A separate switch from LEAD_ENGINE_ENABLED on purpose: the engine
    running is not the same decision as the engine being readable from
    outside."""
    for path in EXTERNAL_ROUTES[:4]:
        response = client.get(path, headers={"x-api-key": TOKEN})
        assert response.status_code == 404
        assert "disabled" in response.json()["error"]


def test_an_unset_key_refuses_rather_than_opening(monkeypatch):
    """An undecided deployment must not default to no auth. That is how
    an open endpoint ships."""
    monkeypatch.setenv(auth.EXTERNAL_ENV, "true")
    monkeypatch.delenv(auth.API_KEY_ENV, raising=False)
    monkeypatch.delenv(auth.API_KEY_ENV_PREFIXED, raising=False)
    response = client.get(f"{V1}/status")
    assert response.status_code == 503
    assert auth.API_KEY_ENV in response.json()["error"]


@pytest.mark.parametrize("path", EXTERNAL_ROUTES)
def test_every_external_route_needs_a_token(path, external_on):
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("header", [
    {"Authorization": f"Bearer {TOKEN}"},
    {"x-api-key": TOKEN},
])
def test_both_token_headers_are_accepted(header, external_on):
    assert client.get(f"{V1}/status", headers=header).status_code == 200


def test_a_wrong_token_is_refused_without_saying_why(external_on):
    response = client.get(f"{V1}/status", headers={"x-api-key": "wrong"})
    assert response.status_code == 401
    assert response.json()["error"] == "Invalid token."


def test_the_rate_limit_bites_but_leaves_room_for_a_reading_a_second(external_on):
    headers = {"x-api-key": TOKEN}
    codes = [client.get(f"{V1}/status", headers=headers).status_code for _ in range(16)]
    assert 200 in codes and 429 in codes
    assert codes.count(200) >= auth.RATE_LIMIT_PER_SECOND


# ---- read only ----------------------------------------------------------

def test_the_external_api_exposes_no_write_method(external_on):
    """Structural, not policy: there is no write path in the package to
    expose. Every route is a GET."""
    from t3_engine.lead_engine import api_v1

    for route in api_v1.router.routes:
        methods = getattr(route, "methods", set())
        assert methods <= {"GET", "HEAD"}, f"{route.path} allows {methods}"


def test_no_mcp_tool_can_write():
    for name, spec in TOOLS.items():
        assert name.startswith("get_"), f"{name} is not a read"
    for tool in tool_list():
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["annotations"]["destructiveHint"] is False


def test_a_trading_tool_simply_does_not_exist():
    for attempt in ("place_order", "close_position", "set_leverage", "withdraw"):
        assert call_tool(attempt, {})["error"].startswith("unknown tool")


# ---- the snapshot an agent actually uses --------------------------------

def _seed_engine():
    import time

    from t3_engine.lead_engine.config import LeadEngineConfig
    from t3_engine.lead_engine.engine import LeadEngine

    engine = LeadEngine(LeadEngineConfig(enabled=True, symbols=["BTCUSDT", "INJUSDT"]))
    engine_module._engine = engine
    now = int(time.time() * 1000) - 60_000
    engine.handle_message("orderbook.50.INJUSDT", {
        "topic": "orderbook.50.INJUSDT", "type": "snapshot", "ts": now,
        "data": {"u": 1,
                 "b": [[str(round(5.70 - i * 0.001, 4)), "100"] for i in range(50)],
                 "a": [[str(round(5.702 + i * 0.001, 4)), "60"] for i in range(50)]}})
    for index in range(120):
        stamp = now + index * 200
        engine.handle_message("publicTrade.INJUSDT", {
            "topic": "publicTrade.INJUSDT", "ts": stamp,
            "data": [{"T": stamp, "S": "Buy" if index % 3 else "Sell", "v": "4",
                      "p": str(round(5.70 + index * 0.0002, 5))}]})
    state = engine.states["INJUSDT"]
    fresh = int(time.time() * 1000)
    state.health.last_book_ms = fresh
    state.health.last_trade_ms = fresh
    state.health.last_ticker_ms = fresh
    return engine


def test_the_snapshot_carries_everything_an_agent_was_promised(external_on):
    _seed_engine()
    body = client.get(f"{V1}/snapshot/INJUSDT", headers={"x-api-key": TOKEN}).json()
    for field in ("symbol", "price", "long_pressure", "short_pressure",
                  "break_score_long", "break_score_short", "structure", "flow",
                  "book", "derivatives", "btc_lead", "microstructure",
                  "open_interest", "liquidations", "smc", "elliott",
                  "support_levels", "resistance_levels", "active_signal",
                  "health", "data_age_ms", "quality", "signals_valid",
                  "server_time", "schema_version"):
        assert field in body, f"{field} missing from the snapshot"
    micro = body["microstructure"]
    for field in ("obi_1", "obi_5", "obi_10", "obi_25", "obi_50", "weighted_obi",
                  "microprice", "cvd", "normalized_delta_5s", "trade_velocity"):
        assert field in micro, f"{field} missing from microstructure"


def test_every_response_says_how_old_its_data_is(external_on):
    _seed_engine()
    body = client.get(f"{V1}/snapshot/INJUSDT", headers={"x-api-key": TOKEN}).json()
    assert body["server_time"] > 0
    assert "book_age_ms" in body and "trade_age_ms" in body
    assert body["quality"] in ("ok", "degraded", "stale", "unavailable")
    assert isinstance(body["signals_valid"], bool)


def test_stale_data_is_never_presented_as_current(external_on):
    """The rule: `quality` and `signals_valid` come from the health gate,
    not from whether the numbers look plausible."""
    engine = _seed_engine()
    state = engine.states["INJUSDT"]
    # Both clocks, because both are real: the exchange said this a minute
    # ago AND it landed a minute ago. Ageing only the exchange stamp would
    # describe a frame that arrived late, which is a different fault.
    state.health.last_book_ms = int(state.health.last_book_ms) - 60_000
    state.health.last_book_receive_ms = int(state.health.last_book_receive_ms
                                            or state.health.last_book_ms) - 60_000
    body = client.get(f"{V1}/snapshot/INJUSDT", headers={"x-api-key": TOKEN}).json()
    assert body["signals_valid"] is False
    assert body["quality"] == "stale"
    assert body["engine_status"] in ("WS_CONNECTED_DATA_STALE", "STALE_DATA", "DEGRADED")


def test_a_break_score_is_not_called_a_probability_until_it_is_one(external_on):
    _seed_engine()
    body = client.get(f"{V1}/snapshot/INJUSDT", headers={"x-api-key": TOKEN}).json()
    for side in ("long", "short"):
        label = body["calibration"][side]
        assert label["kind"] in ("MODEL_SCORE", "PROBABILITY")
        if label["kind"] == "MODEL_SCORE":
            assert label["probability"] is None


def test_the_schema_route_describes_the_api_for_discovery(external_on):
    body = client.get(f"{V1}/schema", headers={"x-api-key": TOKEN}).json()
    assert body["read_only"] is True
    assert f"{V1}/snapshot/{{symbol}}" in body["endpoints"]
    assert "1m" in body["timeframes"] and "4h" in body["timeframes"]
    assert body["rate_limit"]["per_second"] == auth.RATE_LIMIT_PER_SECOND


def _read_stream(max_chunks, headers, interval_ms=200):
    """Consume a bounded number of SSE chunks from the route itself.

    Deliberately NOT through TestClient.stream: the generator only ends on
    a client disconnect, and TestClient's synchronous reader never raises
    one while it is still being consumed, so the loop would run forever.
    Driving the response's own body_iterator lets the test stop after N
    chunks and close it, which is what a real client does anyway."""
    scope = {
        "type": "http", "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": f"{V1}/stream", "raw_path": b"/stream",
        "root_path": "", "query_string": b"", "server": ("test", 80),
        "client": ("test", 1234), "app": server_module.app,
        "headers": [(k.lower().encode(), v.encode())
                    for k, v in headers.items()],
    }

    async def receive():                      # a client that stays connected
        await asyncio.Event().wait()

    async def run():
        request = Request(scope, receive)
        response = await api_v1.stream(request, symbol="INJUSDT",
                                       interval_ms=interval_ms)
        chunks = []
        if response.media_type == "text/event-stream":
            iterator = response.body_iterator
            try:
                async for chunk in iterator:
                    chunks.append(
                        chunk.decode() if isinstance(chunk, bytes) else chunk)
                    if len(chunks) >= max_chunks:
                        break
            finally:
                await iterator.aclose()
        return response, chunks

    return asyncio.run(run())


def test_the_stream_sends_deltas_not_the_whole_state(external_on):
    _seed_engine()
    response, chunks = _read_stream(4, {"x-api-key": TOKEN})
    assert response.status_code == 200
    assert response.media_type == "text/event-stream"
    text = "".join(chunks)
    assert "event: hello" in text
    # A delta, not a full snapshot: the first update carries the compact
    # fields and none of the heavy blocks.
    assert "event: update" in text
    assert "long_pressure" in text
    assert "microstructure" not in text and "deviation_search" not in text
    # Second update onwards is change-only. A quiet engine repeats nothing,
    # so the later chunks are keepalives rather than a resent state.
    later = "".join(chunks[2:])
    assert "long_pressure" not in later


def test_the_stream_refuses_an_unauthenticated_reader(external_on):
    _seed_engine()
    response, chunks = _read_stream(1, {})
    assert response.status_code == 401
    assert chunks == []


# ---- the MCP adapter ----------------------------------------------------

class FakeClient:
    base_url = "http://engine"

    def __init__(self):
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        return {"ok": True, "path": path, "params": params}


def test_the_mcp_handshake_and_tool_list():
    reply = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {}})
    assert reply["result"]["serverInfo"]["name"] == "lead-engine"
    assert "MODEL SCORES" in reply["result"]["instructions"]

    tools = mcp_server.handle({"jsonrpc": "2.0", "id": 2,
                               "method": "tools/list"})["result"]["tools"]
    names = {tool["name"] for tool in tools}
    for required in ("get_lead_engine_status", "get_symbols", "get_market_snapshot",
                     "get_pressure", "get_orderbook_state", "get_trade_flow",
                     "get_derivatives_state", "get_structure", "get_elliott_state",
                     "get_active_signal", "get_recent_signals", "get_candles",
                     "get_indicators", "get_fibonacci_levels", "get_health",
                     "get_multi_tf_snapshot"):
        assert required in names, f"{required} is missing from the MCP tool list"


def test_an_mcp_tool_call_reaches_the_right_endpoint():
    fake = FakeClient()
    reply = mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                               "params": {"name": "get_multi_tf_snapshot",
                                          "arguments": {"symbol": "injusdt"}}}, fake)
    assert reply["result"]["isError"] is False
    assert fake.calls == [("multi-tf/INJUSDT", None)]


def test_an_mcp_error_is_an_answer_not_an_exception():
    reply = mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                               "params": {"name": "get_market_snapshot",
                                          "arguments": {}}}, FakeClient())
    assert reply["result"]["isError"] is True
    assert "needs 'symbol'" in reply["result"]["content"][0]["text"]


def test_an_unknown_method_is_a_jsonrpc_error():
    reply = mcp_server.handle({"jsonrpc": "2.0", "id": 5, "method": "nonsense"})
    assert reply["error"]["code"] == -32601


def test_notifications_get_no_reply():
    assert mcp_server.handle({"jsonrpc": "2.0",
                              "method": "notifications/initialized"}) is None


def test_the_mcp_server_speaks_over_a_pipe():
    out = io.StringIO()
    mcp_server.serve(io.StringIO('{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'),
                     out, FakeClient())
    payload = json.loads(out.getvalue())
    assert len(payload["result"]["tools"]) == len(TOOLS)


def test_the_mcp_package_never_imports_the_engine():
    """The direction that makes the adapter droppable: MCP talks HTTP to
    the API and knows nothing of the engine's internals. If it imported
    them, a broken adapter could take the engine with it."""
    import ast
    import pathlib

    package = pathlib.Path(__file__).resolve().parents[1] / "lead_engine_mcp"
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                assert not module.startswith("t3_engine"), \
                    f"{path.name} imports {module}; the MCP adapter must reach the " \
                    "engine over HTTP only"


def _synthetic_candles(symbol, interval="5m", limit=400, *args, **kwargs):
    """Candles in `fetch_candles`' exact shape, without an exchange.

    The multi-timeframe block is assembled from REST history, and there is
    no network in a test run - so without this every timeframe comes back
    `available: false` and the test asserts nothing."""
    import math
    import time as _time

    from t3_engine.lead_engine.candles_rest import (INTERVAL_SECONDS,
                                                    normalize_interval)

    label = normalize_interval(interval) or "5m"
    seconds = INTERVAL_SECONDS[label]
    now = int(_time.time())
    start = now - (now % seconds) - seconds * (limit - 1)
    price = 5.70
    rows = []
    for index in range(limit):
        price = price * (1 + 0.0009 * math.sin(index / 11.0)) + 0.0004
        high = price * 1.0012
        low = price * 0.9988
        rows.append({"time": start + index * seconds, "open": round(price, 5),
                     "high": round(high, 5), "low": round(low, 5),
                     "close": round(price, 5), "volume": 120.0 + index % 40,
                     "closed": index < limit - 1, "interval": label,
                     "interval_seconds": seconds})
    return rows


def test_the_multi_tf_block_carries_its_reading_at_the_top_level(external_on,
                                                                 monkeypatch):
    """An agent asking "what is the 4h trend" should not have to know that
    the answer lives inside a nested structure block."""
    monkeypatch.setattr(snapshot_module, "fetch_candles", _synthetic_candles)
    _seed_engine()
    body = client.get(f"{V1}/multi-tf/INJUSDT", headers={"x-api-key": TOKEN}).json()
    frames = body["timeframes"]
    assert set(frames) == set(snapshot_module.MTF_TIMEFRAMES)
    for label, block in frames.items():
        assert block["available"] is True, block
        for field in ("trend", "premium_discount", "range_position",
                      "swing_high", "swing_low", "support", "resistance",
                      "bos", "choch", "ema", "ohlc_summary", "current_candle"):
            assert field in block, f"{label} is missing {field}"
        assert set(block["ema"]) == {"ema9", "ema18", "ema50", "ema200"}


def test_every_flow_window_reports_a_bounded_delta(external_on):
    """The unbounded `buy / sell` ratio is the defect this guards. It is
    still reported as a diagnostic, capped so it cannot print a
    seven-figure number, but every window now also carries the bounded
    form that anything downstream should read."""
    from t3_engine.lead_engine.trade_flow import RATIO_DISPLAY_CAP

    _seed_engine()
    body = client.get(f"{V1}/flow/INJUSDT", headers={"x-api-key": TOKEN}).json()
    windows = body["trade_flow"]["windows"]
    assert len(windows) >= 8
    for label, window in windows.items():
        assert -1.0 <= window["normalized_delta"] <= 1.0, (label, window)
        assert window["delta_ratio"] <= RATIO_DISPLAY_CAP, (label, window)
