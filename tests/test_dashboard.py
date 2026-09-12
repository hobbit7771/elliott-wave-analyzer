import asyncio
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import t3_engine.dashboard.server as server_module
from t3_engine.common.models import Scenario, Wave
from t3_engine.common.types import Direction, Timeframe, WaveLabel
from t3_engine.dashboard.server import app, fibonacci_levels_for_scenario, normalize_symbol

client = TestClient(app)


def _wave(label, start_price, end_price, start_time=0, end_time=1000):
    direction = Direction.UP if end_price >= start_price else Direction.DOWN
    return Wave(wave_id=f"w-{label}", parent_wave_id=None, degree=Timeframe.M5, label=label,
                direction=direction, start_timestamp=start_time, end_timestamp=end_time,
                start_price=start_price, end_price=end_price, high=max(start_price, end_price),
                low=min(start_price, end_price))


# ---- Fibonacci overlay (spec follow-up: the only fib-derived lines ever
# drawn were an ACCEPTED signal's TP/SL, which - gated by a confidence
# threshold and hard Elliott rules - are rare, so for long stretches
# nothing fib-related showed at all even though score_fibonacci() was
# using it internally the whole session. fibonacci_levels_for_scenario
# projects levels for whichever wave is expected NEXT, persistently.) ----

def test_fibonacci_levels_projects_wave2_retracement_after_wave1():
    scenario = Scenario(scenario_id="s1", degree=Timeframe.M5,
                         waves=[_wave(WaveLabel.W1, 100.0, 150.0)],
                         next_expected_label=WaveLabel.W2)
    levels = fibonacci_levels_for_scenario(scenario)
    assert len(levels) == 5  # WAVE2_RATIOS
    assert all(lvl["for_wave"] == "2" for lvl in levels)
    # A 0.618 retracement of a 100->150 move lands below 150, above 100.
    ratio_618 = next(lvl for lvl in levels if lvl["ratio"] == 0.618)
    assert 100.0 < ratio_618["price"] < 150.0


def test_fibonacci_levels_projects_wave3_extension_after_wave2():
    scenario = Scenario(scenario_id="s2", degree=Timeframe.M5, waves=[
        _wave(WaveLabel.W1, 100.0, 150.0), _wave(WaveLabel.W2, 150.0, 120.0),
    ], next_expected_label=WaveLabel.W3)
    levels = fibonacci_levels_for_scenario(scenario)
    assert len(levels) == 5  # WAVE3_RATIOS
    assert all(lvl["price"] > 120.0 for lvl in levels)  # projected above wave2's end


def test_fibonacci_levels_empty_when_scenario_missing_or_complete():
    assert fibonacci_levels_for_scenario(None) == []
    complete = Scenario(scenario_id="s3", degree=Timeframe.M5,
                         waves=[_wave(WaveLabel.C, 100.0, 90.0)], next_expected_label=None)
    assert fibonacci_levels_for_scenario(complete) == []


def test_fibonacci_levels_empty_for_wave_a_and_b_no_formula_exists():
    """A and B have no spec-defined Fibonacci ratio in this codebase (only
    2/3/4/5/C do - see fibonacci/calculator.py) - must return nothing
    rather than a made-up level."""
    scenario = Scenario(scenario_id="s4", degree=Timeframe.M5,
                         waves=[_wave(WaveLabel.W5, 100.0, 200.0)], next_expected_label=WaveLabel.A)
    assert fibonacci_levels_for_scenario(scenario) == []


def test_run_backtest_includes_fibonacci_levels_field():
    resp = client.get("/api/run", params={"source": "synthetic", "cycles": 1, "threshold": 50})
    assert resp.status_code == 200
    assert "fibonacci_levels" in resp.json()


# ---- symbol normalization (spec follow-up: dashboard accepted garbage like
# "UNI/USDC" and re-read the input field on every live-poll tick, sending a
# request for whatever partial string the user had typed so far) ----

def test_normalize_symbol_strips_slash_and_uppercases():
    assert normalize_symbol("uni/usdc") == "UNIUSDC"
    assert normalize_symbol("BTC/USDT") == "BTCUSDT"
    assert normalize_symbol(" btc usdt ") == "BTCUSDT"
    assert normalize_symbol("BTCUSDT") == "BTCUSDT"
    assert normalize_symbol("") == ""


def test_run_backtest_bybit_source_rejects_empty_symbol():
    resp = client.get("/api/run", params={"source": "bybit", "symbol": "///"})
    assert resp.status_code == 400


def test_live_start_normalizes_symbol_with_slash():
    resp = client.post("/api/live/stop", json={"symbol": "uni/usdc"})
    assert resp.status_code == 200  # normalizes fine even when nothing is running


def test_run_backtest_bybit_source_surfaces_error_clearly():
    """Bybit is the only exchange this app talks to now (Binance's public
    WS/REST were both effectively unusable in production - see
    dashboard/server.py's module docstring). A Bybit failure must surface
    as a clear, actionable message, not a generic 500/stack trace."""
    request = httpx.Request("GET", "https://api.bybit.com/v5/market/kline")
    response = httpx.Response(451, request=request, text="Unavailable For Legal Reasons")
    error = httpx.HTTPStatusError("451", request=request, response=response)

    with patch.object(server_module.BybitFuturesREST, "get_klines", side_effect=error):
        resp = client.get("/api/run", params={"source": "bybit", "symbol": "BTCUSDT"})
    assert resp.status_code == 502
    assert "bybit" in resp.json()["detail"].lower()


def test_run_backtest_bybit_source_returns_real_candles():
    from t3_engine.common.models import Candle
    from t3_engine.common.types import Timeframe
    sample_candles = [
        Candle(timeframe=Timeframe.M5, open_time=i * 300_000, close_time=(i + 1) * 300_000 - 1,
               open=100.0 + i, high=101.0 + i, low=99.0 + i, close=100.5 + i, volume=10.0)
        for i in range(300)
    ]

    with patch.object(server_module.BybitFuturesREST, "get_klines", return_value=sample_candles):
        resp = client.get("/api/run", params={"source": "bybit", "symbol": "BTCUSDT"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["data_source"] == "bybit"
    assert len(data["candles"]) > 0
    assert data["note"] is None
    assert data["timeframe"] == "5m"
    # Full confirmed ZigZag swing history (not just the current scenario's
    # waves) - lets the frontend draw a zigzag across the WHOLE loaded
    # history for context, instead of numbered waves with nothing behind
    # them (spec follow-up: wave markup only ever appeared at the tail).
    assert "pivots" in data


def test_run_backtest_accepts_a_timeframe_and_uses_it_for_both_the_fetch_and_the_engine_degree():
    from t3_engine.common.models import Candle
    from t3_engine.common.types import Timeframe
    sample_candles = [
        Candle(timeframe=Timeframe.M1, open_time=i * 60_000, close_time=(i + 1) * 60_000 - 1,
               open=100.0 + i, high=101.0 + i, low=99.0 + i, close=100.5 + i, volume=10.0)
        for i in range(300)
    ]

    with patch.object(server_module.BybitFuturesREST, "get_klines", return_value=sample_candles) as mock_get:
        resp = client.get("/api/run", params={"source": "bybit", "symbol": "BTCUSDT", "timeframe": "1m"})
    assert resp.status_code == 200
    mock_get.assert_called_once()
    assert mock_get.call_args.args[1] == Timeframe.M1
    data = resp.json()
    assert data["timeframe"] == "1m"
    # 1m is confirmation-only - 5m/15m/1h/4h are all tradeable (see
    # backtest/engine.py's TRADEABLE_TIMEFRAMES guard) - the response must
    # say so plainly rather than silently showing an always-empty signal
    # list with no explanation.
    assert data["signals"] == []
    assert "1m" in data["note"]


def test_run_backtest_rejects_an_unsupported_timeframe():
    resp = client.get("/api/run", params={"source": "synthetic", "timeframe": "2m"})
    assert resp.status_code == 400


def test_health():
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["build"] == server_module.BUILD_VERSION


def test_index_serves_html():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "lightweight-charts" in resp.text


def test_index_shows_the_same_build_marker_as_health_endpoint():
    """A visible, bump-on-every-deploy marker so a user and a developer
    checking server logs can confirm - without any ambiguity from browser/
    proxy caching or an already-open stale tab - that they're both looking
    at the same actual deploy."""
    resp = client.get("/")
    assert server_module.BUILD_VERSION in resp.text


def test_index_and_service_worker_are_never_cached():
    """A stale HTTP-cached copy of either file silently defeats the
    service-worker-update mechanism (the browser only detects a new SW by
    diffing sw.js bytes it must actually re-fetch) - this was observed in
    production: server logs showed fresh 200s reaching a phone seconds
    after deploy while its already-open tab kept showing old UI text."""
    resp = client.get("/")
    assert "no-store" in resp.headers["cache-control"]

    resp = client.get("/sw.js")
    assert "no-store" in resp.headers["cache-control"]
    assert "controllerchange" not in resp.text  # that logic belongs in index.html, not the worker itself


def test_index_reloads_when_a_new_service_worker_takes_control():
    resp = client.get("/")
    assert "controllerchange" in resp.text


def test_index_ai_tab_is_wired_to_gemini_not_openai():
    """The provider swap has to reach the UI too - a page still asking for
    an sk-... key while the backend talks to Google is a broken feature
    that looks like a working one."""
    resp = client.get("/")
    assert "geminiKey" in resp.text
    assert "aistudio.google.com" in resp.text
    assert "openaiKey" not in resp.text
    assert "sk-..." not in resp.text


def test_index_exposes_the_ai_labelling_mode():
    resp = client.get("/")
    assert "askAiLabel" in resp.text
    assert "/api/ai/label" in resp.text


def test_index_lets_the_model_name_be_edited_without_a_redeploy():
    """Google retires model names for NEW keys while existing ones keep
    working - gemini-2.5-flash 404'd in production this way. The correct
    model is a property of whose key it is, not of this deployment, so it
    has to be editable in the browser."""
    resp = client.get("/")
    assert "geminiModel" in resp.text
    assert server_module.DEFAULT_GEMINI_MODEL in resp.text


def test_index_uses_custom_symbol_dropdown_not_native_datalist():
    """Mobile Safari accepts <input list="..."> silently but never
    actually renders the native datalist suggestion popup - a long-
    standing WebKit gap, not a markup bug. A hand-rolled dropdown
    (#symbolSuggestions) replaces it so autocomplete actually works on
    the phones this app is meant to run on."""
    resp = client.get("/")
    assert "<datalist" not in resp.text
    assert "symbolSuggestions" in resp.text


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
    # Subwave counting (spec follow-up: "waves and subwaves should be
    # accounted for") - see BacktestEngine.subwave_history.
    assert "subwave_history" in data


def test_run_backtest_accepts_a_custom_starting_equity():
    """Follow-up: "give unlimited capital" isn't a real lever in a
    %-of-equity risk model (risk_per_wave is a fraction of equity, so
    everything scales proportionally) - what it honestly reduces to is
    letting the starting balance be configured, which this exposes."""
    resp = client.get("/api/run", params={"source": "synthetic", "cycles": 1, "threshold": 50, "equity": 1_000_000})
    assert resp.status_code == 200
    assert resp.json()["final_equity"] >= 900_000  # started near 1,000,000, not the 10,000 default


def test_run_backtest_response_is_json_serializable_end_to_end():
    resp = client.get("/api/run", params={"source": "synthetic", "cycles": 2, "threshold": 40})
    assert resp.status_code == 200
    data = resp.json()
    for sig in data["signals"]:
        assert sig["decision"] in ("SIGNAL_ACCEPTED", "SIGNAL_REJECTED")
    for pos in data["closed_positions"]:
        assert "realized_pnl" in pos


# ---- live pipeline endpoints ----
# `run_live` (the method server.py actually calls, driving Bybit's public
# WS - see live_loop.py) is patched to a never-ending no-op coroutine
# instead of a real WebSocket connection - this environment blocks
# outbound access to real exchanges (see README), and these tests only
# need to verify the FastAPI task bookkeeping (start/status/stop), not a
# real socket (that's covered separately in test_pipeline_live_loop.py).

async def _fake_run_live(self):
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
    with patch.object(server_module.LiveTradingEngine, "run_live", _fake_run_live):
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
            assert state.json()["tradeable"] is True
            assert "pivots" in state.json()

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
    with patch.object(server_module.LiveTradingEngine, "run_live", _fake_run_live):
        with TestClient(app) as c:
            c.post("/api/live/start", json={"symbol": "formingusdt"})

            from t3_engine.candle_builder.aggregator import Trade
            engine = server_module._live_engines["FORMINGUSDT"]
            engine.on_trade(Trade(timestamp=0, price=100.0, quantity=1.0, is_buyer_maker=False))

            state = c.get("/api/live/state", params={"symbol": "formingusdt", "timeframe": "5m"}).json()
            assert len(state["candles"]) == 1
            assert state["candles"][0]["close"] == 100.0
            assert state["waiting_for_first_candle"] is False
            assert state["trades_received"] == 1

            status = c.get("/api/live/status").json()
            assert status["FORMINGUSDT"]["trades_received"] == 1

            c.post("/api/live/stop", json={"symbol": "formingusdt"})


def test_live_state_supports_context_timeframes_marked_not_tradeable():
    """Spec follow-up: the TF picker offers 1m/5m/15m/1h/4h in both history
    and live mode now, not just 5m/15m - live/live_loop.py's default
    DISPLAY_TIMEFRAMES was widened accordingly. 1m stays confirmation-only
    (5m/15m/1h/4h are all tradeable per the project owner's explicit
    instruction to enable 1h/4h trading too), so the state response must
    say so explicitly (tradeable: false) rather than pretend it generates
    real entries the way the tradeable degrees do."""
    with patch.object(server_module.LiveTradingEngine, "run_live", _fake_run_live):
        with TestClient(app) as c:
            c.post("/api/live/start", json={"symbol": "ctxusdt"})

            state = c.get("/api/live/state", params={"symbol": "ctxusdt", "timeframe": "1m"})
            assert state.status_code == 200
            assert state.json()["tradeable"] is False

            for tf in ("5m", "1h", "4h"):
                tradeable_state = c.get("/api/live/state", params={"symbol": "ctxusdt", "timeframe": tf})
                assert tradeable_state.json()["tradeable"] is True

            c.post("/api/live/stop", json={"symbol": "ctxusdt"})


def test_live_stop_when_not_running():
    resp = client.post("/api/live/stop", json={"symbol": "GHOSTUSDT"})
    assert resp.json()["status"] == "not_running"


# ---- symbol list ----

def _reset_symbols_cache():
    server_module._symbols_cache["symbols"] = None
    server_module._symbols_cache["fetched_at"] = 0.0


def test_list_symbols_returns_and_caches():
    _reset_symbols_cache()

    with patch.object(server_module.BybitFuturesREST, "list_symbols", return_value=["BTCUSDT", "ETHUSDT"]) as mock_list:
        resp1 = client.get("/api/symbols")
        assert resp1.status_code == 200
        assert resp1.json() == {"symbols": ["BTCUSDT", "ETHUSDT"], "cached": False, "source": "live"}

        resp2 = client.get("/api/symbols")
        assert resp2.json()["cached"] is True
        mock_list.assert_called_once()  # second call served from cache, no second Bybit hit


def test_list_symbols_falls_back_to_static_list_on_bybit_failure():
    """The picker must never come back empty just because Bybit can't be
    reached - it should serve the static fallback list instead, clearly
    tagged, so the frontend always has something to suggest."""
    _reset_symbols_cache()
    request = httpx.Request("GET", "https://api.bybit.com/v5/market/instruments-info")

    with patch.object(server_module.BybitFuturesREST, "list_symbols",
                      side_effect=httpx.ConnectError("boom", request=request)):
        resp = client.get("/api/symbols")
    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "fallback"
    assert "BTCUSDT" in data["symbols"]
    assert "bybit" in data["reason"].lower()
    _reset_symbols_cache()


# ---- AI advisor endpoint ----

def test_ai_advice_requires_key():
    resp = client.post("/api/ai/advice", json={"api_key": "", "context": {"wave": "3"}})
    assert resp.status_code == 502


def test_ai_advice_success_with_mocked_gemini():
    class FakeResponse:
        text = "Looks like a reasonable wave 3 setup, watch for extension risk."
        model = "gemini-3.6-flash"
        raw = {}

    with patch.object(server_module, "request_commentary", return_value=FakeResponse()):
        resp = client.post("/api/ai/advice", json={"api_key": "AIza-test", "context": {"wave": "3"}})
        assert resp.status_code == 200
        assert "wave 3" in resp.json()["commentary"].lower()


# ---- AI labelling endpoint ----
# The whole design claim here is "the model proposes, the server
# disposes". These tests mock the model's answer and check what the
# ENDPOINT does with it - a proposal is only ever as good as the
# validation standing behind it.

class _FakeProposal:
    def __init__(self, waves, reasoning="because"):
        self.waves = waves
        self.reasoning = reasoning
        self.model = "gemini-3.6-flash"
        self.raw = {}


def test_ai_label_requires_key():
    resp = client.post("/api/ai/label", json={"api_key": "", "source": "synthetic", "cycles": 1})
    assert resp.status_code == 502


def test_ai_label_rejects_a_hallucinated_pivot_index_instead_of_drawing_it():
    """The headline guarantee: an index the model invented cannot become a
    wave on the chart. It comes back rejected, with a reason."""
    nonsense = [{"label": "1", "start_pivot_index": 0, "end_pivot_index": 99999}]

    with patch.object(server_module, "request_wave_count", return_value=_FakeProposal(nonsense)):
        resp = client.post("/api/ai/label", json={"api_key": "AIza-test", "source": "synthetic", "cycles": 2})
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert data["rejected"] is True
    assert "does not exist" in data["reason"]
    assert data["waves"] == []


def _first_index_of_kind(pivots, kind):
    return next(i for i, p in enumerate(pivots) if p["kind"] == kind)


def test_ai_label_rejects_a_count_that_breaks_a_hard_elliott_rule():
    """Structurally well-formed but mathematically impossible: real pivot
    indices, canonical labels, correct alternation - and an impulse whose
    wave 2 retraces straight past the start of wave 1. The server rebuilds
    it from its OWN pivots, the hard rules fire, and the answer is a
    labelled failure rather than a drawn wave."""
    def label_five_consecutive(api_key, pivots, direction, model=None):
        start = _first_index_of_kind(pivots, "HIGH" if direction == "DOWN" else "LOW")
        return _FakeProposal([
            {"label": label, "start_pivot_index": start + i, "end_pivot_index": start + i + 1}
            for i, label in enumerate(["1", "2", "3", "4", "5"])
        ])

    with patch.object(server_module, "request_wave_count", side_effect=label_five_consecutive):
        resp = client.post("/api/ai/label", json={"api_key": "AIza-test", "source": "synthetic", "cycles": 2})
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert data["rejected"] is False          # well-formed input, illegal count
    assert data["broken_rule"] == "WAVE2_OVER_100_PCT"
    # The partial waves come back for explanation, but `valid` is what
    # gates drawing them as a count - it is never True here.
    assert len(data["waves"]) < 5


def test_ai_label_accepts_and_returns_server_built_waves_for_a_legal_count():
    """On the happy path the waves handed back are built from the
    server's pivots - the model supplied indices, never prices."""
    captured = {}

    def label_from_real_pivots(api_key, pivots, direction, model=None):
        captured["pivots"] = pivots
        captured["direction"] = direction
        start = _first_index_of_kind(pivots, "HIGH" if direction == "DOWN" else "LOW")
        return _FakeProposal([{"label": "1", "start_pivot_index": start, "end_pivot_index": start + 1}])

    with patch.object(server_module, "request_wave_count", side_effect=label_from_real_pivots):
        resp = client.post("/api/ai/label", json={"api_key": "AIza-test", "source": "synthetic", "cycles": 2})
    assert resp.status_code == 200
    data = resp.json()
    assert data["rejected"] is False
    assert data["valid"] is True
    assert len(data["waves"]) == 1
    # The model was handed indices into the server's own pivot list.
    assert captured["pivots"][0]["index"] == 0
    assert set(captured["pivots"][0]) == {"index", "time", "price", "kind"}
    # And the wave's prices came back from those pivots, not from the model.
    start = _first_index_of_kind(captured["pivots"], "HIGH" if captured["direction"] == "DOWN" else "LOW")
    assert data["waves"][0]["start_price"] == captured["pivots"][start]["price"]


def test_ai_label_never_takes_pivots_from_the_caller():
    """If the client could supply pivots, "the server validated the
    indices" would be a claim about the caller's data, not the chart.
    Client-sent pivots must be ignored outright."""
    def echo_pivot_count(api_key, pivots, direction, model=None):
        return _FakeProposal([{"label": "1", "start_pivot_index": 0, "end_pivot_index": 1}],
                             reasoning=f"saw {len(pivots)} pivots")

    with patch.object(server_module, "request_wave_count", side_effect=echo_pivot_count):
        resp = client.post("/api/ai/label", json={
            "api_key": "AIza-test", "source": "synthetic", "cycles": 2,
            "pivots": [{"index": 0, "price": 1.0, "kind": "LOW"}],  # attacker-supplied, must be ignored
        })
    assert resp.status_code == 200
    assert "saw 1 pivots" not in resp.json()["reasoning"]
