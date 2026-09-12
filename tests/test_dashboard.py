import asyncio
import time
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import t3_engine.dashboard.server as server_module
from t3_engine.dashboard.server import app, normalize_symbol

client = TestClient(app)


# ---- symbol normalization (spec follow-up: dashboard accepted garbage like
# "UNI/USDC" and re-read the input field on every live-poll tick, sending a
# request for whatever partial string the user had typed so far) ----

def test_normalize_symbol_strips_slash_and_uppercases():
    assert normalize_symbol("uni/usdc") == "UNIUSDC"
    assert normalize_symbol("BTC/USDT") == "BTCUSDT"
    assert normalize_symbol(" btc usdt ") == "BTCUSDT"
    assert normalize_symbol("BTCUSDT") == "BTCUSDT"
    assert normalize_symbol("") == ""


def test_run_backtest_binance_source_rejects_empty_symbol():
    resp = client.get("/api/run", params={"source": "binance", "symbol": "///"})
    assert resp.status_code == 400


def test_live_start_normalizes_symbol_with_slash():
    resp = client.post("/api/live/stop", json={"symbol": "uni/usdc"})
    assert resp.status_code == 200  # normalizes fine even when nothing is running


def test_run_backtest_binance_source_surfaces_451_region_block_clearly():
    """Reproduces the exact failure seen in production logs: Binance
    returns HTTP 451 for requests from a blocked region (e.g. a US-hosted
    Render service). The dashboard must surface this as a clear, actionable
    message instead of a generic 500/stack trace."""
    request = httpx.Request("GET", "https://fapi.binance.com/fapi/v1/klines")
    response = httpx.Response(451, request=request, text="Unavailable For Legal Reasons")
    error = httpx.HTTPStatusError("451", request=request, response=response)

    with patch.object(server_module.BinanceFuturesREST, "get_klines", side_effect=error):
        resp = client.get("/api/run", params={"source": "binance", "symbol": "BTCUSDT"})
    assert resp.status_code == 451
    assert "region" in resp.json()["detail"].lower()


def test_health():
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_index_serves_html():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "lightweight-charts" in resp.text


def test_run_backtest_synthetic_returns_real_candles_and_metrics():
    resp = client.get("/api/run", params={"source": "synthetic", "cycles": 1, "threshold": 50})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["candles"]) > 0
    first = data["candles"][0]
    assert set(first.keys()) == {"time", "open", "high", "low", "close", "volume"}
    assert "metrics" in data
    assert "overall" in data["metrics"]
    assert data["note"] is not None  # synthetic disclosure note present


def test_run_backtest_response_is_json_serializable_end_to_end():
    resp = client.get("/api/run", params={"source": "synthetic", "cycles": 2, "threshold": 40})
    assert resp.status_code == 200
    data = resp.json()
    for sig in data["signals"]:
        assert sig["decision"] in ("SIGNAL_ACCEPTED", "SIGNAL_REJECTED")
    for pos in data["closed_positions"]:
        assert "realized_pnl" in pos


# ---- live pipeline endpoints ----
# `run_live_binance` is patched to a never-ending no-op coroutine instead of
# a real WebSocket connection - this environment blocks outbound access to
# Binance (see README), and these tests only need to verify the FastAPI
# task bookkeeping (start/status/stop), not a real socket.

async def _fake_run_live_binance(self):
    await asyncio.Event().wait()  # blocks forever until the task is cancelled


def test_live_state_404_when_not_started():
    resp = client.get("/api/live/state", params={"symbol": "NOPEUSDT", "timeframe": "5m"})
    assert resp.status_code == 404


def test_live_start_status_stop_lifecycle():
    # `with TestClient(...) as c:` keeps ONE persistent event loop/portal
    # for the whole block (this is what makes lifespan + long-lived
    # background tasks work) - a bare `client.post(...)` per call would
    # spin up a fresh loop each time and cancel our background task
    # between calls, which is a TestClient-only artifact: a real ASGI
    # server (uvicorn) keeps a single event loop for the process, so
    # `asyncio.create_task` in a request handler behaves as expected there.
    with patch.object(server_module.LiveTradingEngine, "run_live_binance", _fake_run_live_binance):
        with TestClient(app) as c:
            resp = c.post("/api/live/start", json={"symbol": "testusdt"})
            assert resp.status_code == 200
            assert resp.json()["status"] == "started"

            status = c.get("/api/live/status").json()
            assert "TESTUSDT" in status
            assert status["TESTUSDT"]["running"] is True

            state = c.get("/api/live/state", params={"symbol": "testusdt", "timeframe": "5m"})
            assert state.status_code == 200
            assert state.json()["symbol"] == "TESTUSDT"

            again = c.post("/api/live/start", json={"symbol": "testusdt"})
            assert again.json()["status"] == "already_running"

            stop = c.post("/api/live/stop", json={"symbol": "testusdt"})
            assert stop.json()["status"] == "stopped"

            status_after = c.get("/api/live/status").json()
            assert "TESTUSDT" not in status_after


def test_live_state_includes_forming_candle_before_first_close():
    """The very first bar of a live session hasn't closed yet (a 15m bar
    only appears after 15 real minutes) - the chart should still show the
    in-progress bar updating, and the response should say so explicitly,
    rather than the frontend just showing '0 candles' with no explanation."""
    with patch.object(server_module.LiveTradingEngine, "run_live_binance", _fake_run_live_binance):
        with TestClient(app) as c:
            c.post("/api/live/start", json={"symbol": "formingusdt"})

            from t3_engine.candle_builder.aggregator import Trade
            engine = server_module._live_engines["FORMINGUSDT"]
            engine.candle_builder.on_trade(Trade(timestamp=0, price=100.0, quantity=1.0, is_buyer_maker=False))

            state = c.get("/api/live/state", params={"symbol": "formingusdt", "timeframe": "5m"}).json()
            assert len(state["candles"]) == 1
            assert state["candles"][0]["close"] == 100.0
            assert state["waiting_for_first_candle"] is False

            c.post("/api/live/stop", json={"symbol": "formingusdt"})


def test_live_stop_when_not_running():
    resp = client.post("/api/live/stop", json={"symbol": "GHOSTUSDT"})
    assert resp.json()["status"] == "not_running"


# ---- symbol list ----

def _reset_binance_state():
    server_module._symbols_cache["symbols"] = None
    server_module._symbols_cache["fetched_at"] = 0.0
    server_module._binance_backoff_until = 0.0
    server_module._binance_backoff_message = None


def test_list_symbols_returns_and_caches():
    _reset_binance_state()

    with patch.object(server_module.BinanceFuturesREST, "list_symbols", return_value=["BTCUSDT", "ETHUSDT"]) as mock_list:
        resp1 = client.get("/api/symbols")
        assert resp1.status_code == 200
        assert resp1.json() == {"symbols": ["BTCUSDT", "ETHUSDT"], "cached": False}

        resp2 = client.get("/api/symbols")
        assert resp2.json()["cached"] is True
        mock_list.assert_called_once()  # second call served from cache, no second Binance hit


def test_list_symbols_surfaces_network_error():
    _reset_binance_state()
    request = httpx.Request("GET", "https://fapi.binance.com/fapi/v1/exchangeInfo")

    with patch.object(server_module.BinanceFuturesREST, "list_symbols", side_effect=httpx.ConnectError("boom", request=request)):
        resp = client.get("/api/symbols")
    assert resp.status_code == 502


def test_list_symbols_surfaces_451():
    _reset_binance_state()
    request = httpx.Request("GET", "https://fapi.binance.com/fapi/v1/exchangeInfo")
    response = httpx.Response(451, request=request, text="blocked")
    error = httpx.HTTPStatusError("451", request=request, response=response)

    with patch.object(server_module.BinanceFuturesREST, "list_symbols", side_effect=error):
        resp = client.get("/api/symbols")
    assert resp.status_code == 451


def test_418_triggers_shared_backoff_across_endpoints():
    """A 418 ('I'm a teapot' - Binance's documented IP-ban response) must
    stop ALL Binance-touching endpoints from calling out again until the
    cooldown expires - repeating requests during a ban is what turns a
    short ban into a long one, per Binance's own rate-limit docs."""
    _reset_binance_state()
    request = httpx.Request("GET", "https://fapi.binance.com/fapi/v1/exchangeInfo")
    response = httpx.Response(418, request=request, text="teapot", headers={"Retry-After": "30"})
    error = httpx.HTTPStatusError("418", request=request, response=response)

    with patch.object(server_module.BinanceFuturesREST, "list_symbols", side_effect=error) as mock_list:
        first = client.get("/api/symbols")
        assert first.status_code == 418
        assert "teapot" in first.json()["detail"].lower() or "banned" in first.json()["detail"].lower()

        # second call must NOT hit Binance again - it's blocked by the
        # in-process cooldown the first 418 just registered
        second = client.get("/api/symbols")
        assert second.status_code == 429
        assert "remaining" in second.json()["detail"].lower()
        mock_list.assert_called_once()

    # the cooldown is shared: /api/run's binance path is blocked too,
    # without ever touching BinanceFuturesREST.get_klines
    with patch.object(server_module.BinanceFuturesREST, "get_klines") as mock_klines:
        resp = client.get("/api/run", params={"source": "binance", "symbol": "BTCUSDT"})
        assert resp.status_code == 429
        mock_klines.assert_not_called()

    _reset_binance_state()


def test_retry_after_header_sets_backoff_duration():
    _reset_binance_state()
    request = httpx.Request("GET", "https://fapi.binance.com/fapi/v1/exchangeInfo")
    response = httpx.Response(418, request=request, text="teapot", headers={"Retry-After": "5"})
    error = httpx.HTTPStatusError("418", request=request, response=response)

    with patch.object(server_module.BinanceFuturesREST, "list_symbols", side_effect=error):
        client.get("/api/symbols")

    remaining = server_module._binance_backoff_until - time.time()
    assert 0 < remaining <= 5.5
    _reset_binance_state()


# ---- AI advisor endpoint ----

def test_ai_advice_requires_key():
    resp = client.post("/api/ai/advice", json={"api_key": "", "context": {"wave": "3"}})
    assert resp.status_code == 502


def test_ai_advice_success_with_mocked_openai():
    class FakeResponse:
        text = "Looks like a reasonable wave 3 setup, watch for extension risk."
        model = "gpt-4o-mini"
        raw = {}

    with patch.object(server_module, "request_commentary", return_value=FakeResponse()):
        resp = client.post("/api/ai/advice", json={"api_key": "sk-test", "context": {"wave": "3"}})
        assert resp.status_code == 200
        assert "wave 3" in resp.json()["commentary"].lower()
