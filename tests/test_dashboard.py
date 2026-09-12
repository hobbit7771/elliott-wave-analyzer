import asyncio
from unittest.mock import patch

from fastapi.testclient import TestClient

import t3_engine.dashboard.server as server_module
from t3_engine.dashboard.server import app

client = TestClient(app)


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


def test_live_stop_when_not_running():
    resp = client.post("/api/live/stop", json={"symbol": "GHOSTUSDT"})
    assert resp.json()["status"] == "not_running"


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
