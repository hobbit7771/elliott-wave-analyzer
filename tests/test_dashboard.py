from fastapi.testclient import TestClient

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
