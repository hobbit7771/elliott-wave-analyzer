import asyncio
from unittest.mock import patch

import httpx
from types import SimpleNamespace

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


def test_index_ai_tab_is_wired_to_the_current_provider():
    """The provider swap has to reach the UI too - a page still asking for
    a Google key while the backend talks to OpenRouter is a broken feature
    that looks like a working one. This is the third provider this app has
    been pointed at, so the check is kept sharp: the old names must be
    GONE, not merely joined by the new one."""
    resp = client.get("/")
    assert "aiKey" in resp.text
    assert "openrouter.ai/api/v1" in resp.text
    assert "geminiKey" not in resp.text
    assert "aistudio.google.com" not in resp.text
    assert "openaiKey" not in resp.text
    assert "orcarouter" not in resp.text
    assert "integrate.api.nvidia.com" not in resp.text


def test_a_key_saved_for_an_old_provider_is_not_reused_for_the_new_one():
    """localStorage survives a provider swap. Forwarding a leftover key
    from the previous provider would fail as "invalid key", which reads as
    "the app is broken" rather than "that key is for the wrong service"."""
    resp = client.get("/")
    assert "t3_openrouter2_key" in resp.text
    assert "t3_gemini_key" not in resp.text
    assert "t3_nvidia_key" not in resp.text
    assert "t3_orcarouter_key" not in resp.text


def test_index_lets_the_api_base_url_be_edited():
    """The endpoint could not be verified from the build sandbox, so it is
    a field rather than a constant - if the real path differs, that is a
    paste, not a redeploy."""
    resp = client.get("/")
    assert "aiBaseUrl" in resp.text
    assert server_module.DEFAULT_AI_API_BASE in resp.text


def test_index_exposes_the_ai_labelling_mode():
    resp = client.get("/")
    assert "askAiLabel" in resp.text
    assert "/api/ai/label" in resp.text


def test_index_lets_the_model_name_be_edited_without_a_redeploy():
    """A router carries hundreds of models whose ids, prices and free tiers
    change week to week. The correct model is a property of whose key it
    is, not of this deployment, so it has to be editable in the browser."""
    resp = client.get("/")
    assert "aiModel" in resp.text
    assert server_module.DEFAULT_AI_MODEL in resp.text


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


def test_ai_advice_success_with_a_mocked_model():
    class FakeResponse:
        text = "Looks like a reasonable wave 3 setup, watch for extension risk."
        model = "~openai/gpt-astra-latest"
        raw = {}

    with patch.object(server_module, "request_commentary", return_value=FakeResponse()):
        resp = client.post("/api/ai/advice", json={"api_key": "sk-test", "context": {"wave": "3"}})
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
        self.model = "~openai/gpt-astra-latest"
        self.raw = {}


def test_ai_label_requires_key():
    resp = client.post("/api/ai/label", json={"api_key": "", "source": "synthetic", "cycles": 1})
    assert resp.status_code == 502


def test_ai_label_rejects_a_hallucinated_pivot_index_instead_of_drawing_it():
    """The headline guarantee: an index the model invented cannot become a
    wave on the chart. It comes back rejected, with a reason."""
    nonsense = [{"label": "1", "start_pivot_index": 0, "end_pivot_index": 99999}]

    with patch.object(server_module, "request_wave_count", return_value=_FakeProposal(nonsense)):
        resp = client.post("/api/ai/label", json={"api_key": "sk-test", "source": "synthetic", "cycles": 2})
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
    def label_five_consecutive(api_key, pivots, direction, model=None, base_url=None, timeout=None,
                             reasoning_effort=None, thinking=None):
        # Find a place where the geometry ACTUALLY breaks the wave-2 rule -
        # five consecutive pivots whose third one has retraced past the
        # first - rather than trusting the fixture to happen to contain one
        # at its very first swing. It did until the synthetic source
        # started alternating direction per timeframe, and then this test
        # was asserting an accident of the data instead of the rule.
        anchor = "HIGH" if direction == "DOWN" else "LOW"
        start = None
        for i, pivot in enumerate(pivots[:-5]):
            if pivot["kind"] != anchor:
                continue
            retraced_past_start = (pivots[i + 2]["price"] <= pivot["price"]) if anchor == "LOW" \
                else (pivots[i + 2]["price"] >= pivot["price"])
            if retraced_past_start:
                start = i
                break
        assert start is not None, "no wave-2-over-100% arrangement in this fixture"
        return _FakeProposal([
            {"label": label, "start_pivot_index": start + i, "end_pivot_index": start + i + 1}
            for i, label in enumerate(["1", "2", "3", "4", "5"])
        ])

    with patch.object(server_module, "request_wave_count", side_effect=label_five_consecutive):
        resp = client.post("/api/ai/label", json={"api_key": "sk-test", "source": "synthetic", "cycles": 2})
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

    def label_from_real_pivots(api_key, pivots, direction, model=None, base_url=None, timeout=None,
                             reasoning_effort=None, thinking=None):
        captured["pivots"] = pivots
        captured["direction"] = direction
        start = _first_index_of_kind(pivots, "HIGH" if direction == "DOWN" else "LOW")
        return _FakeProposal([{"label": "1", "start_pivot_index": start, "end_pivot_index": start + 1}])

    with patch.object(server_module, "request_wave_count", side_effect=label_from_real_pivots):
        resp = client.post("/api/ai/label", json={"api_key": "sk-test", "source": "synthetic", "cycles": 2})
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
    def echo_pivot_count(api_key, pivots, direction, model=None, base_url=None, timeout=None,
                             reasoning_effort=None, thinking=None):
        return _FakeProposal([{"label": "1", "start_pivot_index": 0, "end_pivot_index": 1}],
                             reasoning=f"saw {len(pivots)} pivots")

    with patch.object(server_module, "request_wave_count", side_effect=echo_pivot_count):
        resp = client.post("/api/ai/label", json={
            "api_key": "sk-test", "source": "synthetic", "cycles": 2,
            "pivots": [{"index": 0, "price": 1.0, "kind": "LOW"}],  # attacker-supplied, must be ignored
        })
    assert resp.status_code == 200
    assert "saw 1 pivots" not in resp.json()["reasoning"]


# ---------------------------------------------------------------------------
# AI analyst: a clean chart, an agent, and the same trust boundary
# ---------------------------------------------------------------------------

def test_index_exposes_the_analyst_tab_with_its_own_chart():
    """The analyst gets a SEPARATE chart element. Reusing the main one
    would put the engine's markup under the model's count, and a model
    shown an existing markup stops being an independent read."""
    resp = client.get("/")
    assert 'data-tab="analyst"' in resp.text
    assert 'id="analystChart"' in resp.text
    assert "/api/ai/analyst" in resp.text
    assert "runAnalyst" in resp.text


def test_analyst_requires_a_key_like_every_other_ai_path():
    resp = client.post("/api/ai/analyst", json={"api_key": "", "source": "synthetic", "cycles": 1})
    assert resp.status_code == 502


class _FakeAnalystResult:
    def __init__(self, accepted, rejected=None, note="", error=""):
        self.accepted = accepted
        self.rejected = rejected or []
        self.error = error
        self.transcript = []
        self.projection = None
        self.coverage = {}
        self.summary = "Five waves up look complete."
        self.reasoning = "Wave 3 is the longest."
        self.steps = []
        self.model = "~openai/gpt-astra-latest"
        self.steps_used = 3
        self.usage = {}
        self.finished = True
        self.note = note

    @property
    def waves(self):
        return [{**w, "structure": s.get("structure")}
                for s in self.accepted for w in s.get("waves", [])]


def test_analyst_returns_the_exact_candles_it_analysed():
    """The tab draws the series returned WITH the answer rather than
    fetching its own, so the count can never be rendered over a chart that
    differs from the one the agent worked."""
    seen = {}

    def fake_run(api_key, candles, degree, **kwargs):
        seen["candles"] = candles
        return _FakeAnalystResult([])

    with patch.object(server_module, "run_analyst", side_effect=fake_run):
        resp = client.post("/api/ai/analyst",
                           json={"api_key": "sk-test", "source": "synthetic", "cycles": 2})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["candles"]) == len(seen["candles"])
    assert data["candles"][0]["time"] == seen["candles"][0].open_time // 1000


def test_analyst_reports_rejected_structures_rather_than_hiding_them():
    """"The model proposed this and the rule engine threw it out" is the
    most useful line on the panel - it is the evidence that validation runs
    at all."""
    rejected = [{"position": 0, "structure": "IMPULSE", "valid": False,
                 "broken_rule": "WAVE4_OVERLAPS_WAVE1", "reason": "Wave 4 low entered Wave 1 territory"}]
    with patch.object(server_module, "run_analyst",
                      return_value=_FakeAnalystResult([], rejected=rejected)):
        resp = client.post("/api/ai/analyst",
                           json={"api_key": "sk-test", "source": "synthetic", "cycles": 2})
    data = resp.json()
    assert data["accepted"] == []
    assert data["rejected"][0]["broken_rule"] == "WAVE4_OVERLAPS_WAVE1"
    assert data["waves"] == []


def test_analyst_passes_the_model_name_and_step_budget_through():
    seen = {}

    def fake_run(api_key, candles, degree, **kwargs):
        seen.update(kwargs)
        return _FakeAnalystResult([])

    with patch.object(server_module, "run_analyst", side_effect=fake_run):
        client.post("/api/ai/analyst", json={
            "api_key": "sk-test", "source": "synthetic", "cycles": 2,
            "model": "anthropic/some-other-model", "max_steps": 7,
        })
    assert seen["model"] == "anthropic/some-other-model"
    assert seen["max_steps"] == 7


def test_analyst_step_budget_is_bounded_by_the_api_not_only_by_the_ui():
    resp = client.post("/api/ai/analyst", json={
        "api_key": "sk-test", "source": "synthetic", "cycles": 2, "max_steps": 5000})
    assert resp.status_code == 422


def test_switching_to_the_analyst_tab_actually_reveals_its_chart():
    """#analystChart is display:none in the stylesheet, so clearing the
    inline style falls back to that rule and the tab shows an empty gap.
    It has to be set to a real display value - caught in the browser, kept
    here so it cannot come back."""
    resp = client.get("/")
    assert "onAnalyst ? 'block' : 'none'" in resp.text
    assert "onAnalyst ? '' : 'none'" not in resp.text


def test_the_api_base_url_reaches_every_ai_endpoint():
    """A base URL the UI keeps to itself would be a field that looks like
    it works and doesn't."""
    seen = {}

    def fake_run(api_key, candles, degree, **kwargs):
        seen["analyst"] = kwargs.get("base_url")
        return _FakeAnalystResult([])

    with patch.object(server_module, "run_analyst", side_effect=fake_run):
        client.post("/api/ai/analyst", json={
            "api_key": "sk-test", "source": "synthetic", "cycles": 2,
            "base_url": "https://openrouter.ai/v2"})
    assert seen["analyst"] == "https://openrouter.ai/v2"

    def fake_commentary(api_key, context, model=None, base_url=None, timeout=None,
                        reasoning_effort=None, thinking=None):
        seen["advice"] = base_url
        return SimpleNamespace(text="ok", model=model)

    with patch.object(server_module, "request_commentary", side_effect=fake_commentary):
        client.post("/api/ai/advice", json={
            "api_key": "sk-test", "context": {}, "base_url": "https://openrouter.ai/v2"})
    assert seen["advice"] == "https://openrouter.ai/v2"


def test_a_partial_run_returns_its_transcript_instead_of_nothing():
    """A run that stalls part way still did real work, and the transcript is
    information ("it listed the swings, measured wave 3, then the model
    stopped answering"). Throwing that away and showing an empty result
    would read as a finished analysis that found nothing."""
    partial = _FakeAnalystResult([], note="Stopped at step 3.",
                                 error="The model did not answer within 180s.")
    partial.finished = False
    partial.steps_used = 2
    with patch.object(server_module, "run_analyst", return_value=partial):
        resp = client.post("/api/ai/analyst",
                           json={"api_key": "sk-test", "source": "synthetic", "cycles": 2})
    data = resp.json()
    assert resp.status_code == 200
    assert data["finished"] is False
    assert "did not answer within" in data["error"]
    assert data["steps_used"] == 2


def test_ping_reports_a_broken_setup_as_an_answer_not_as_a_server_error():
    """A 5xx here would read in the UI as "the dashboard is broken". The
    request was fine; the answer is "your key/URL/model doesn't work"."""
    with patch.object(server_module, "ai_ping",
                      side_effect=server_module.AIAdvisorError("OrcaRouter API error 401: bad key")):
        resp = client.post("/api/ai/ping", json={"api_key": "sk-wrong"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "401" in body["error"]


def test_ping_passes_the_whole_setup_through_so_it_tests_what_the_real_run_uses():
    """A connection check against different settings than the real run uses
    would be worse than none at all."""
    seen = {}

    def fake_ping(api_key, model=None, base_url=None, timeout=None):
        seen.update({"key": api_key, "model": model, "base_url": base_url})
        return {"ok": True, "model": model, "endpoint": base_url, "answer": "ok"}

    with patch.object(server_module, "ai_ping", side_effect=fake_ping):
        client.post("/api/ai/ping", json={
            "api_key": "sk-test", "model": "~openai/gpt-astra-latest",
            "base_url": "https://openrouter.ai/api/v1"})
    assert seen["key"] == "sk-test"
    assert seen["model"] == "~openai/gpt-astra-latest"
    assert seen["base_url"] == "https://openrouter.ai/api/v1"


def test_index_exposes_the_connection_test_and_timeout_controls():
    resp = client.get("/")
    assert "pingAi" in resp.text
    assert "/api/ai/ping" in resp.text
    assert "aiTimeout" in resp.text


def test_the_analyst_error_text_is_rendered_not_only_the_step_count():
    """"Stopped after 2 steps" without the reason is not a diagnosis. The
    error line is the one that says whether to wait longer, change the
    model, or fix the URL."""
    resp = client.get("/")
    assert "data.error ?" in resp.text
    assert 'class="note error"' in resp.text


# ---------------------------------------------------------------------------
# Server-side API key
# ---------------------------------------------------------------------------

def test_ai_config_reports_whether_a_server_key_exists_but_never_the_key():
    """The UI needs to know a blank field will work. It must never learn
    what the key is - that endpoint is reachable by anyone who can open the
    dashboard."""
    with patch.object(server_module, "SERVER_AI_KEY", "nvapi-super-secret"):
        resp = client.get("/api/ai/config")
    body = resp.json()
    assert body["server_key"] is True
    assert "nvapi-super-secret" not in resp.text
    assert body["provider"] == "OpenRouter"


def test_a_request_without_a_key_falls_back_to_the_server_key():
    seen = {}

    def fake_run(api_key, candles, degree, **kwargs):
        seen["key"] = api_key
        return _FakeAnalystResult([])

    with patch.object(server_module, "run_analyst", side_effect=fake_run), \
         patch.object(server_module, "resolve_api_key", side_effect=lambda k: k or "server-key"):
        client.post("/api/ai/analyst", json={"source": "synthetic", "cycles": 2})
    assert seen["key"] == "server-key"


def test_a_key_in_the_request_wins_over_the_server_key():
    """Otherwise a user pasting their own key would silently spend the
    server owner's quota instead."""
    from t3_engine.ai_advisor.advisor import resolve_api_key

    with patch("t3_engine.ai_advisor.advisor.DEFAULT_API_KEY", "server-key"):
        assert resolve_api_key("nvapi-mine") == "nvapi-mine"
        assert resolve_api_key("  ") == "server-key"
        assert resolve_api_key(None) == "server-key"


def test_reasoning_effort_reaches_the_analyst():
    seen = {}

    def fake_run(api_key, candles, degree, **kwargs):
        seen.update(kwargs)
        return _FakeAnalystResult([])

    with patch.object(server_module, "run_analyst", side_effect=fake_run):
        client.post("/api/ai/analyst", json={
            "api_key": "sk-or-test", "source": "synthetic", "cycles": 2,
            "reasoning_effort": "max"})
    assert seen["reasoning_effort"] == "max"


def test_index_offers_the_reasoning_effort_control():
    resp = client.get("/")
    assert "aiEffort" in resp.text
    assert "/api/ai/config" in resp.text


def test_the_step_transcript_shows_arguments_not_just_tool_names():
    """"What it actually did" has to say WHICH swings it listed and WHICH
    legs it measured. A bare list of verbs answers nothing."""
    resp = client.get("/")
    assert "formatStepArgs" in resp.text
    assert "deviation_pct=3" in resp.text or "formatStepArgs(s.args)" in resp.text


def test_the_transcript_section_is_never_hidden_when_a_run_returns():
    """"It made no tool calls at all" is itself the finding - a model that
    answers in prose instead of working the chart. An absent section reads
    as a rendering bug rather than as that answer."""
    resp = client.get("/")
    assert "No tool calls were made" in resp.text


def test_the_analyst_returns_the_conversation_so_a_stall_can_be_explained():
    """"Why did it stop at step 2" is unanswerable from a list of tool
    names. The model's own words and its reasoning travel with the
    result."""
    partial = _FakeAnalystResult([], note="used all 2 steps")
    partial.finished = False
    partial.steps_used = 2
    partial.transcript = [
        {"role": "assistant", "step": 1, "text": "Looking at the skeleton.",
         "reasoning": "Coarse first.", "tool_calls": ["list_pivots"]},
        {"role": "tool", "step": 1, "name": "list_pivots", "args": {"deviation_pct": 3.0},
         "result": "22 pivots at 3.0% deviation"},
        {"role": "system", "step": 2, "text": "This is your LAST step."},
    ]
    with patch.object(server_module, "run_analyst", return_value=partial):
        resp = client.post("/api/ai/analyst",
                           json={"api_key": "sk-or-test", "source": "synthetic", "cycles": 2})
    data = resp.json()
    assert [e["role"] for e in data["transcript"]] == ["assistant", "tool", "system"]
    assert data["transcript"][0]["reasoning"] == "Coarse first."


def test_index_renders_the_conversation_including_the_models_thinking():
    resp = client.get("/")
    assert "renderAnalystChat" in resp.text
    assert "analystChat" in resp.text
    assert "entry.reasoning" in resp.text


def test_the_connection_check_uses_the_configured_timeout_not_a_hardcoded_one():
    """A check that gives up sooner than the real calls do reports a
    working setup as broken - which is exactly how a slow reasoning model
    looked."""
    resp = client.get("/")
    assert "timeout: aiTimeout()" in resp.text
    assert "timeout: 60 }" not in resp.text


def test_the_connection_check_surfaces_time_to_first_token():
    """"It works" is not the useful answer. The analyst pays that number
    once per step."""
    resp = client.get("/")
    assert "seconds_to_first_token" in resp.text
    assert "data.advice" in resp.text


def test_diagnose_runs_the_model_free_check_first():
    """The catalogue listing is what splits "our side" from "the model's
    side", so it has to run even when the chat probe is the interesting
    part."""
    calls = []

    def fake_access(key, base_url=None, timeout=None, model=""):
        calls.append("access")
        return {"ok": True, "url": base_url, "seconds": 0.4, "model_count": 3, "models": []}

    def fake_diagnose(key, model=None, base_url=None, timeout=None):
        calls.append("chat")
        return {"status": 200, "verdict": "Healthy"}

    with patch.object(server_module, "ai_check_access", side_effect=fake_access), \
         patch.object(server_module, "ai_diagnose", side_effect=fake_diagnose):
        resp = client.post("/api/ai/diagnose", json={"api_key": "sk-or-test"})
    assert calls == ["access", "chat"]
    body = resp.json()
    assert body["access"]["model_count"] == 3
    assert body["chat"]["verdict"] == "Healthy"


def test_a_failing_access_check_does_not_stop_the_chat_probe():
    """When access is broken, the chat probe's raw status corroborates
    why - dropping it would lose the corroboration."""
    with patch.object(server_module, "ai_check_access",
                      side_effect=server_module.AIAdvisorError("401 invalid key")), \
         patch.object(server_module, "ai_diagnose",
                      return_value={"status": 401, "verdict": "rejected outright"}):
        resp = client.post("/api/ai/diagnose", json={"api_key": "sk-or-bad"})
    body = resp.json()
    assert body["access"]["ok"] is False
    assert body["chat"]["status"] == 401


def test_index_exposes_the_diagnose_button():
    resp = client.get("/")
    assert "diagnoseAi" in resp.text
    assert "/api/ai/diagnose" in resp.text


def test_thinking_is_a_tri_state_because_not_sending_the_field_is_a_real_choice():
    """A model that has never heard of chat_template_kwargs answers 400
    rather than ignoring it, so "omit it" cannot be conflated with "off"."""
    from t3_engine.dashboard.server import parse_thinking

    assert parse_thinking("off") is False
    assert parse_thinking("on") is True
    assert parse_thinking("") is None
    assert parse_thinking("nonsense") is None


def test_thinking_defaults_to_on_now_that_the_model_is_paid():
    """It was off while the free models sat behind a shared GPU pool. On a
    paid model, thinking is the reason to use it."""
    seen = {}

    def fake_run(api_key, candles, degree, **kwargs):
        seen.update(kwargs)
        return _FakeAnalystResult([])

    with patch.object(server_module, "run_analyst", side_effect=fake_run):
        client.post("/api/ai/analyst", json={"api_key": "sk-or-test", "source": "synthetic",
                                             "cycles": 2})
    assert seen["thinking"] is True

    with patch.object(server_module, "run_analyst", side_effect=fake_run):
        client.post("/api/ai/analyst", json={"api_key": "sk-or-test", "source": "synthetic",
                                             "cycles": 2, "thinking": "off"})
    assert seen["thinking"] is False


def test_diagnose_runs_the_variant_probe_that_names_the_cause():
    with patch.object(server_module, "ai_check_access",
                      return_value={"ok": True, "seconds": 0.08, "model_count": 82, "models": []}), \
         patch.object(server_module, "ai_diagnose", return_value={"status": 200}), \
         patch.object(server_module, "ai_probe_variants",
                      return_value={"variants": [{"variant": "thinking off, streamed", "ok": True}],
                                    "verdict": "Thinking mode is the cause"}):
        resp = client.post("/api/ai/diagnose", json={"api_key": "sk-or-test"})
    assert "Thinking mode is the cause" in resp.json()["probe"]["verdict"]


def test_index_exposes_the_thinking_switch():
    resp = client.get("/")
    assert "aiThinking" in resp.text
    assert "reasoning: {enabled, effort}" in resp.text
    assert "chat_template_kwargs" not in resp.text      # a NIM-ism, not the router's field


def test_index_says_reasoning_is_carried_across_steps():
    """The agent loop is many turns long. Without reasoning_details passed
    back, every step starts its thinking over - worse answers, larger bill."""
    resp = client.get("/")
    assert "reasoning_details" in resp.text


# ---------------------------------------------------------------------------
# kumo-relational: trade quality, not a second analyst
# ---------------------------------------------------------------------------

def test_signal_quality_refuses_rather_than_inventing_a_number():
    """The synthetic fixture produces few or no closed trades, so the
    honest answer is "not enough history" - a 422 with the reason, not a
    probability nobody should trust."""
    resp = client.post("/api/ai/signal-quality",
                       json={"api_key": "sk-or-test", "source": "synthetic", "cycles": 2})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "closed trade" in detail or "No signals to score" in detail


def test_signal_quality_returns_the_context_size_with_every_probability():
    """A probability without the size and balance of the history behind it
    invites more trust than it has earned."""
    from t3_engine.ai_advisor.relational import RelationalPrediction, RelationalResult

    fake = RelationalResult(
        predictions=[RelationalPrediction(signal_id="sig-1", win_probability=0.64, prediction=True)],
        context_trades=41, wins_in_context=18, model="kumo-relational")
    with patch.object(server_module, "predict_trade_quality", return_value=fake):
        resp = client.post("/api/ai/signal-quality",
                           json={"api_key": "sk-or-test", "source": "synthetic", "cycles": 2})
    body = resp.json()
    assert body["context_trades"] == 41
    assert body["wins_in_context"] == 18
    assert body["scored"][0]["win_probability"] == 0.64


def test_signal_quality_uses_the_relational_host_not_the_chat_one():
    """Two different NVIDIA hosts with the same key; crossing them produces
    a 404 that reads like a broken model id."""
    from t3_engine.ai_advisor.relational import DEFAULT_RELATIONAL_URL

    assert "ai.api.nvidia.com" in DEFAULT_RELATIONAL_URL
    assert "integrate.api.nvidia.com" not in DEFAULT_RELATIONAL_URL


def test_diagnose_reports_whether_the_chosen_model_can_run_the_analyst():
    """The AI Analyst needs tool calling and most free models lack it. The
    catalogue knows; guessing from the model's name does not."""
    with patch.object(server_module, "ai_check_access",
                      return_value={"ok": True, "seconds": 0.2, "model_count": 310,
                                    "models": [], "tool_capable": ["a/b:free"],
                                    "tool_capable_count": 1, "model_supports_tools": False,
                                    "checked_model": "~openai/gpt-astra-latest"}), \
         patch.object(server_module, "ai_diagnose", return_value={"status": 200}), \
         patch.object(server_module, "ai_probe_variants", return_value={"variants": [], "verdict": ""}):
        resp = client.post("/api/ai/diagnose", json={
            "api_key": "sk-or-test", "model": "~openai/gpt-astra-latest"})
    access = resp.json()["access"]
    assert access["model_supports_tools"] is False
    assert access["tool_capable_count"] == 1


def test_index_shows_the_tool_calling_verdict_where_the_model_is_chosen():
    resp = client.get("/")
    assert "model_supports_tools" in resp.text
    assert "the AI Analyst can run" in resp.text


def test_the_catalogue_only_call_skips_the_slow_probe():
    """Filling the model list must not also spend four 25s variant probes -
    that is a minute of waiting for a list of names."""
    probed = []

    with patch.object(server_module, "ai_check_access",
                      return_value={"ok": True, "seconds": 0.1, "model_count": 2, "models": [],
                                    "tool_capable": ["a/b:free"], "tool_capable_count": 1,
                                    "model_supports_tools": True, "checked_model": "a/b:free"}), \
         patch.object(server_module, "ai_probe_variants", side_effect=lambda *a, **k: probed.append(1)), \
         patch.object(server_module, "ai_diagnose", side_effect=lambda *a, **k: probed.append(1)):
        resp = client.post("/api/ai/diagnose", json={"api_key": "sk-or-test", "probe": False})

    assert probed == []
    assert resp.json()["access"]["tool_capable"] == ["a/b:free"]


def test_index_can_fill_the_model_field_from_the_catalogue():
    """The loop this replaces: try a model, watch it be busy or lack tool
    calling, go find another one by hand."""
    resp = client.get("/")
    assert "fillModels" in resp.text
    assert "tool_capable" in resp.text
    assert "includes(':free')" in resp.text      # free ones first, not a surprise invoice


def test_the_analyst_returns_where_the_count_says_price_goes_next():
    """A count that stops at the last pivot answers "what happened". The
    reason to count at all is what it implies comes next."""
    fake = _FakeAnalystResult([])
    fake.projection = {"next_label": "5", "basis": "wave 1 length projected from the end of wave 4",
                       "from_time": 1000, "from_price": 138.0,
                       "targets": [{"ratio": 0.618, "price": 150.4},
                                   {"ratio": 1.0, "price": 158.0}]}
    fake.coverage = {"covered_fraction": 0.82, "gaps": []}

    with patch.object(server_module, "run_analyst", return_value=fake):
        resp = client.post("/api/ai/analyst",
                           json={"api_key": "sk-or-test", "source": "synthetic", "cycles": 2})
    body = resp.json()
    assert body["projection"]["next_label"] == "5"
    assert body["projection"]["targets"][1]["price"] == 158.0
    assert body["coverage"]["covered_fraction"] == 0.82


def test_index_draws_the_projection_dashed_and_says_who_computed_it():
    """Dashed and in its own colour because it is the one thing on the
    chart that has not happened yet."""
    resp = client.get("/")
    assert "drawAnalystProjection" in resp.text
    assert "lineStyle: 2" in resp.text
    assert "not supplied by the model" in resp.text


def test_index_reports_how_much_of_the_chart_got_labelled():
    resp = client.get("/")
    assert "of the chart labelled" in resp.text


# ---------------------------------------------------------------------------
# Multi-timeframe: one opinion from four charts, nothing counted twice
# ---------------------------------------------------------------------------

def test_multi_timeframe_returns_a_verdict_and_says_what_it_reused():
    """The saving has to be visible rather than claimed - otherwise nobody
    can tell whether the cache is doing anything."""
    from t3_engine.ai_advisor.multi_timeframe import MultiTimeframeResult, TimeframeAnalysis

    fake = MultiTimeframeResult(
        symbol="INJUSDT", model="~openai/gpt-astra-latest",
        note="Reused the saved analysis for 1h, 4h", reused_timeframes=["1h", "4h"],
        recomputed_timeframes=["5m", "15m"],
        verdict={"trend": "UP", "conviction": "medium", "headline": "h",
                 "technical_analysis": "long text"},
        per_timeframe=[TimeframeAnalysis(timeframe="5m", reused=False, candles=1000,
                                         last_candle_time=1, accepted=[{"structure": "IMPULSE"}],
                                         coverage={"covered_fraction": 0.9})])

    with patch.object(server_module, "run_multi_timeframe", return_value=fake):
        resp = client.post("/api/ai/multi", json={"api_key": "sk-or-test", "source": "synthetic"})
    body = resp.json()
    assert body["verdict"]["trend"] == "UP"
    assert body["reused"] == ["1h", "4h"]
    assert body["recomputed"] == ["5m", "15m"]
    assert body["timeframes"][0]["structures"] == 1


def test_the_multi_endpoint_covers_exactly_the_four_timeframes_asked_for():
    from t3_engine.ai_advisor.multi_timeframe import MTF_TIMEFRAMES

    assert [tf.value for tf in MTF_TIMEFRAMES] == ["5m", "15m", "1h", "4h"]


def test_clearing_the_cache_reports_how_much_it_forgot():
    with patch.object(server_module.analysis_store, "clear", return_value=4):
        resp = client.post("/api/ai/multi/clear",
                           json={"source": "bybit", "symbol": "INJUSDT"})
    assert resp.json()["cleared"] == 4


def test_the_single_timeframe_analyst_also_gets_measured_probabilities():
    """A percentage shown in one view and missing in the other would look
    like one of them is guessing."""
    fake = _FakeAnalystResult([])
    fake.projection = {"next_label": "5", "basis": "b", "from_time": 1, "from_price": 1.0,
                       "targets": [{"ratio": 0.618, "price": 2.0}, {"ratio": 1.618, "price": 3.0}]}
    with patch.object(server_module, "run_analyst", return_value=fake):
        resp = client.post("/api/ai/analyst",
                           json={"api_key": "sk-or-test", "source": "synthetic", "cycles": 6})
    targets = resp.json()["projection"]["targets"]
    # Either every target carries a measured probability, or the response
    # says why none is shown. Never a bare number with nothing behind it.
    body = resp.json()["projection"]
    assert all("probability" in t for t in targets) or "odds_note" in body


def test_index_exposes_the_multi_timeframe_tab():
    resp = client.get("/")
    assert 'data-tab="mtf"' in resp.text
    assert "/api/ai/multi" in resp.text
    assert "base rate over" in resp.text        # the sample size travels with the percentage
    assert "Conflicts" in resp.text             # disagreement is shown, not smoothed away


# ---- live sessions start from history, not from nothing ----------------
# A live session used to begin with an empty chart and no past at all, so
# the model's markup (and the engine's own count) could not be shown until
# enough NEW candles had closed - a full bar on 5m, days on 4h. Live now
# backfills from Bybit REST and replays the saved AI analysis for the same
# series on top, then continues streaming into it.

def _seed_store(tmp_path, monkeypatch):
    from t3_engine.ai_advisor import analysis_store
    url = f"sqlite:///{tmp_path / 'live-cache.db'}"
    monkeypatch.setattr(analysis_store, "DEFAULT_DATABASE_URL", url)
    monkeypatch.setattr(analysis_store, "_factory", None)
    return analysis_store


def test_seed_live_history_backfills_every_tracked_timeframe(monkeypatch):
    from t3_engine.backtest.synthetic_data import generate_synthetic_series
    from t3_engine.pipeline.live_loop import LiveTradingEngine
    candles = generate_synthetic_series(num_cycles=1)
    asked = []

    class FakeREST:
        def get_klines(self, symbol, timeframe, limit=200):
            asked.append((symbol, timeframe, limit))
            return candles
        def close(self):
            pass

    engine = LiveTradingEngine(symbol="BTCUSDT", trading_timeframes=(Timeframe.M5, Timeframe.M15),
                              log_dir="/tmp/t3_test_logs")
    with patch.object(server_module, "BybitFuturesREST", FakeREST):
        filled = server_module.seed_live_history(engine, "BTCUSDT", 500)

    assert filled == {"5m": len(candles), "15m": len(candles)}
    assert [a[2] for a in asked] == [500, 500]
    assert len(engine.history[Timeframe.M5]) == len(candles)


def test_seed_live_history_degrades_rather_than_refusing_to_start(monkeypatch):
    """A backfill that cannot be fetched must not stop the live stream:
    trading off a chart with no history is worse than trading off one that
    starts empty, but refusing to connect at all is worse than both."""
    from t3_engine.pipeline.live_loop import LiveTradingEngine
    from t3_engine.market_data.bybit_rest_client import BybitAPIError

    class FailingREST:
        def get_klines(self, symbol, timeframe, limit=200):
            raise BybitAPIError(10001, "Bybit said no")
        def close(self):
            pass

    engine = LiveTradingEngine(symbol="BTCUSDT", trading_timeframes=(Timeframe.M5,),
                              log_dir="/tmp/t3_test_logs")
    with patch.object(server_module, "BybitFuturesREST", FailingREST):
        filled = server_module.seed_live_history(engine, "BTCUSDT", 500)

    assert filled == {"5m": 0}          # named as empty, not silently absent


def test_seed_live_history_skipped_entirely_when_backfill_is_zero():
    from t3_engine.pipeline.live_loop import LiveTradingEngine

    class ExplodingREST:
        def __init__(self):
            raise AssertionError("no REST call should be made for backfill=0")

    engine = LiveTradingEngine(symbol="BTCUSDT", trading_timeframes=(Timeframe.M5,),
                              log_dir="/tmp/t3_test_logs")
    with patch.object(server_module, "BybitFuturesREST", ExplodingREST):
        assert server_module.seed_live_history(engine, "BTCUSDT", 0) == {}


def test_live_ai_analysis_reports_how_far_behind_the_saved_count_is(tmp_path, monkeypatch):
    from t3_engine.backtest.synthetic_data import generate_synthetic_series
    store = _seed_store(tmp_path, monkeypatch)
    candles = generate_synthetic_series(num_cycles=1)
    # analysed as of ten candles ago
    store.save("bybit", "BTCUSDT", "5m", candles[-11].open_time, len(candles) - 10,
               {"accepted": [{"structure": "IMPULSE", "waves": []}], "projection": {"next_label": "3"},
                "coverage": {"covered_fraction": 0.8}, "summary": "up"}, model="test-model")

    fresh = server_module.live_ai_analysis("BTCUSDT", Timeframe.M5, candles)
    assert fresh["candles_since"] == 10
    assert fresh["stale"] is True
    assert fresh["model"] == "test-model"
    assert fresh["accepted"][0]["structure"] == "IMPULSE"

    # ...and current when nothing newer has closed since
    up_to_date = server_module.live_ai_analysis("BTCUSDT", Timeframe.M5, candles[:-10])
    assert up_to_date["candles_since"] == 0
    assert up_to_date["stale"] is False


def test_live_ai_analysis_is_none_when_nothing_was_ever_analysed(tmp_path, monkeypatch):
    _seed_store(tmp_path, monkeypatch)
    assert server_module.live_ai_analysis("NOSUCHUSDT", Timeframe.M5, []) is None


def test_analyst_endpoint_caches_its_result_for_reuse(tmp_path, monkeypatch):
    """The same count must not be paid for twice: what the analyst tab
    produces is stored under the key the multi-timeframe view and the live
    chart read."""
    store = _seed_store(tmp_path, monkeypatch)
    accepted = [{"structure": "IMPULSE", "waves": [
        {"label": "1", "start_time": 1, "end_time": 2, "start_price": 1.0, "end_price": 2.0,
         "direction": "UP"}]}]
    fake = _FakeAnalystResult(accepted)
    with patch.object(server_module, "run_analyst", return_value=fake):
        resp = client.post("/api/ai/analyst",
                           json={"api_key": "sk-or-test", "source": "synthetic",
                                 "symbol": "SYNTHETIC", "cycles": 2})
    assert resp.status_code == 200
    cached = store.load("synthetic", "SYNTHETIC-DEMO", "5m")
    assert cached is not None
    assert cached.payload["accepted"] == accepted
    assert cached.model == "~openai/gpt-astra-latest"


def test_analyst_endpoint_does_not_cache_an_empty_count(tmp_path, monkeypatch):
    """Caching a run that produced nothing would suppress the retry that
    might have worked."""
    store = _seed_store(tmp_path, monkeypatch)
    with patch.object(server_module, "run_analyst", return_value=_FakeAnalystResult([])):
        client.post("/api/ai/analyst",
                    json={"api_key": "sk-or-test", "source": "synthetic", "cycles": 2})
    assert store.load("synthetic", "SYNTHETIC-DEMO", "5m") is None


def test_index_draws_the_saved_ai_count_on_the_live_chart():
    resp = client.get("/")
    assert "drawLiveAiAnalysis" in resp.text
    assert "ai_analysis" in resp.text
    # the age of the saved count travels with it - a stale reading must not
    # be able to pass for a current one
    assert "AI count age" in resp.text
    assert "backfill" in resp.text


# ---- long runs outlive the request that asked for them -----------------
# Measured on the real deploy: POST /api/ai/multi took 12m13s, answered
# correctly, and the page showed "Load failed" because the phone had
# dropped that connection minutes earlier - after the tokens were spent.
# The POST now starts a job and returns immediately.

def _clean_jobs():
    from t3_engine.ai_advisor import jobs
    jobs.clear_all()
    return jobs


def _await_job(job_id, timeout=10.0):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get("/api/ai/job", params={"job_id": job_id})
        body = resp.json()
        if body["status"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError("job never finished")


def test_starting_an_analyst_run_returns_a_job_id_immediately():
    import time
    _clean_jobs()
    accepted = [{"structure": "IMPULSE", "waves": [
        {"label": "1", "start_time": 1, "end_time": 2, "start_price": 1.0, "end_price": 2.0,
         "direction": "UP"}]}]

    def slow_run(*a, **k):
        time.sleep(0.3)
        return _FakeAnalystResult(accepted)

    with patch.object(server_module, "run_analyst", side_effect=slow_run):
        started = time.monotonic()
        resp = client.post("/api/ai/analyst/start",
                           json={"api_key": "sk-or-test", "source": "synthetic", "cycles": 2})
        # The whole point: the response does not wait for the run.
        assert time.monotonic() - started < 0.3
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "running"
        assert body["joined"] is False

        finished = _await_job(body["job_id"])
    assert finished["status"] == "done"
    assert finished["result"]["accepted"] == accepted


def test_a_second_identical_request_joins_the_run_instead_of_paying_twice():
    import threading
    import time
    _clean_jobs()
    gate = threading.Event()
    calls = []

    def blocking_run(*a, **k):
        calls.append(1)
        gate.wait(5)
        return _FakeAnalystResult([])

    with patch.object(server_module, "run_analyst", side_effect=blocking_run):
        first = client.post("/api/ai/analyst/start",
                            json={"source": "synthetic", "cycles": 2}).json()
        time.sleep(0.1)
        second = client.post("/api/ai/analyst/start",
                             json={"source": "synthetic", "cycles": 2}).json()
        assert second["joined"] is True
        assert second["job_id"] == first["job_id"]
        gate.set()
        _await_job(first["job_id"])
    assert len(calls) == 1          # the model was asked ONCE


def test_a_failed_run_is_collectable_with_its_reason():
    """A run that dies must be readable afterwards. A job that simply
    disappears is indistinguishable from one still working."""
    _clean_jobs()
    with patch.object(server_module, "run_analyst",
                      side_effect=server_module.AIAdvisorError("the provider said no")):
        body = client.post("/api/ai/analyst/start",
                           json={"source": "synthetic", "cycles": 2}).json()
        finished = _await_job(body["job_id"])
    assert finished["status"] == "error"
    assert "the provider said no" in finished["error"]


def test_polling_only_sends_progress_the_caller_does_not_have():
    import threading
    _clean_jobs()
    gate = threading.Event()

    def run(*a, **k):
        on_progress = k.get("on_progress")
        on_progress("step one")
        on_progress("step two")
        gate.wait(5)
        return _FakeAnalystResult([])

    with patch.object(server_module, "run_analyst", side_effect=run):
        body = client.post("/api/ai/analyst/start",
                           json={"source": "synthetic", "cycles": 2}).json()
        import time
        time.sleep(0.2)
        first = client.get("/api/ai/job", params={"job_id": body["job_id"]}).json()
        texts = [line["text"] for line in first["progress"]]
        assert "step one" in texts and "step two" in texts
        # A twelve-minute run must not re-send its whole transcript every
        # few seconds.
        again = client.get("/api/ai/job",
                           params={"job_id": body["job_id"],
                                   "since": first["progress_total"]}).json()
        assert again["progress"] == []
        assert again["progress_total"] == first["progress_total"]
        gate.set()
        _await_job(body["job_id"])


def test_an_unknown_job_says_why_rather_than_looking_finished():
    _clean_jobs()
    resp = client.get("/api/ai/job", params={"job_id": "nosuchjob"})
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert "restart" in detail and "saved" in detail      # and what survives it


def test_the_multi_timeframe_verdict_is_saved_and_can_be_collected_later(tmp_path, monkeypatch):
    """The other half of surviving a dropped connection: a run that
    finished while the page was gone is still there to be shown."""
    store = _seed_store(tmp_path, monkeypatch)
    _clean_jobs()

    class FakeResult:
        symbol = "SYNTHETIC-DEMO"
        model = "~openai/gpt-astra-latest"
        note = "Recomputed everything."
        reused_timeframes = []
        recomputed_timeframes = ["5m"]
        verdict = {"trend": "UP", "headline": "Impulsive."}
        per_timeframe = [SimpleNamespace(
            timeframe="5m", reused=False, candles=500, coverage={"covered_fraction": 0.8},
            summary="up", error="", steps_used=7, last_candle_time=12345,
            accepted=[{"structure": "IMPULSE", "waves": []}], projection=None)]

    with patch.object(server_module, "run_multi_timeframe", return_value=FakeResult()):
        body = client.post("/api/ai/multi/start",
                           json={"source": "synthetic", "symbol": "SYNTHETIC-DEMO"}).json()
        finished = _await_job(body["job_id"])
    assert finished["status"] == "done"
    assert finished["result"]["verdict"]["trend"] == "UP"

    saved = client.get("/api/ai/multi/saved",
                       params={"source": "synthetic", "symbol": "SYNTHETIC-DEMO"}).json()
    assert saved["saved"]["verdict"]["trend"] == "UP"
    assert store.load("synthetic", "SYNTHETIC-DEMO", server_module.MTF_CACHE_KEY) is not None


def test_no_saved_verdict_reads_as_nothing_saved_not_as_an_error(tmp_path, monkeypatch):
    _seed_store(tmp_path, monkeypatch)
    resp = client.get("/api/ai/multi/saved",
                      params={"source": "bybit", "symbol": "NOSUCHUSDT"})
    assert resp.status_code == 200
    assert resp.json()["saved"] is None


def test_index_follows_jobs_instead_of_holding_a_twelve_minute_request_open():
    resp = client.get("/")
    assert "/api/ai/analyst/start" in resp.text
    assert "/api/ai/multi/start" in resp.text
    assert "/api/ai/job?job_id=" in resp.text
    assert "resumeJobs" in resp.text          # a reload reattaches to a running analysis
    assert "Live progress" in resp.text       # and it is visible while it runs


# ---- live is the agent's chart, and only the agent's --------------------

def _live_engine_for(symbol, monkeypatch, ai_only=True):
    from t3_engine.backtest.synthetic_data import generate_synthetic_series
    from t3_engine.pipeline.live_loop import LiveTradingEngine
    candles = generate_synthetic_series(num_cycles=1)
    engine = LiveTradingEngine(symbol=symbol, trading_timeframes=(Timeframe.M5,),
                               log_dir="/tmp/t3_test_logs", ai_only=ai_only)
    engine.seed_history(Timeframe.M5, candles)
    monkeypatch.setitem(server_module._live_engines, symbol, engine)
    return engine, candles


def test_live_state_sends_no_engine_markup_in_ai_only_mode(tmp_path, monkeypatch):
    """A second count drawn underneath the agent's is exactly the
    superimposed mess the analyst tab exists to avoid."""
    _seed_store(tmp_path, monkeypatch)
    _live_engine_for("AIONLYUSDT", monkeypatch)
    body = client.get("/api/live/state",
                      params={"symbol": "AIONLYUSDT", "timeframe": "5m"}).json()
    assert body["ai_only"] is True
    assert body["pivots"] == []
    assert body["confirmed_chain"] == []
    assert body["structure_events"] == []
    assert body["scenarios"] == []          # no AI count saved yet either
    # Subwaves and the Fibonacci grid come from the AGENT'S count in this
    # mode, so with no count yet they are empty for that reason, not
    # because the engine's were suppressed.
    assert body["subwave_history"] == []
    assert body["fibonacci_levels"] == []
    assert body["candles"]                  # the candles themselves are still there


def test_live_state_shows_the_engines_own_count_when_ai_only_is_off(tmp_path, monkeypatch):
    _seed_store(tmp_path, monkeypatch)
    _live_engine_for("ENGINEUSDT", monkeypatch, ai_only=False)
    body = client.get("/api/live/state",
                      params={"symbol": "ENGINEUSDT", "timeframe": "5m"}).json()
    assert body["ai_only"] is False
    assert body["pivots"]


def test_live_state_installs_the_saved_ai_count_and_reports_it_as_the_scenario(tmp_path, monkeypatch):
    store = _seed_store(tmp_path, monkeypatch)
    engine, candles = _live_engine_for("COUNTUSDT", monkeypatch)

    def leg(label, a, b):
        return {"label": label, "start_time": candles[a].open_time // 1000,
                "end_time": candles[b].open_time // 1000,
                "start_price": candles[a].close, "end_price": candles[b].close,
                "direction": "UP" if candles[b].close >= candles[a].close else "DOWN"}

    store.save("bybit", "COUNTUSDT", "5m", candles[-1].open_time, len(candles), {
        "accepted": [{"structure": "IMPULSE", "waves": [
            leg("1", 0, 20), leg("2", 20, 30), leg("3", 30, 60), leg("4", 60, 70)]}],
        "projection": {"next_label": "5",
                       "targets": [{"ratio": 1.0, "price": candles[-1].close * 1.2,
                                    "primary": True}]},
        "coverage": {}, "summary": "impulse up",
    }, model="test-model")

    body = client.get("/api/live/state",
                      params={"symbol": "COUNTUSDT", "timeframe": "5m"}).json()
    # The scenario the live panel shows IS the agent's count - the thing
    # actually being traded, not a second opinion.
    assert len(body["scenarios"]) == 1
    assert body["scenarios"][0]["waves"][-1]["label"] == "5"
    assert engine.engines[Timeframe.M5].ai_scenario is not None


def test_saved_counts_list_accumulates_every_analysed_timeframe(tmp_path, monkeypatch):
    """A multi-timeframe pass saves four counts. The analyst tab used to
    forget all of them the moment it was reopened."""
    store = _seed_store(tmp_path, monkeypatch)
    for tf, structures in (("4h", 2), ("5m", 1), ("1h", 3)):
        store.save("bybit", "INJUSDT", tf, 1000, 500, {
            "accepted": [{"structure": "IMPULSE", "waves": []}] * structures,
            "projection": {"next_label": "3"}, "coverage": {"covered_fraction": 0.7},
            "summary": f"{tf} read",
        }, model="m")
    # the multi-timeframe verdict shares the cache but is not a timeframe
    store.save("bybit", "INJUSDT", server_module.MTF_CACHE_KEY, 1000, 500, {"verdict": {}}, model="m")

    body = client.get("/api/ai/saved", params={"source": "bybit", "symbol": "INJUSDT"}).json()
    assert [row["timeframe"] for row in body["timeframes"]] == ["5m", "1h", "4h"]
    assert body["timeframes"][2]["structures"] == 2
    assert body["timeframes"][0]["projection"]["next_label"] == "3"


def test_saved_counts_list_is_empty_not_an_error_for_a_fresh_instrument(tmp_path, monkeypatch):
    _seed_store(tmp_path, monkeypatch)
    resp = client.get("/api/ai/saved", params={"source": "bybit", "symbol": "FRESHUSDT"})
    assert resp.status_code == 200
    assert resp.json()["timeframes"] == []


def test_index_stops_repainting_the_whole_live_series_every_poll():
    """The live chart ticked, flickered and drifted because every four
    second poll replaced 1000 candles and rebuilt every overlay."""
    resp = client.get("/")
    assert "candleSeries.update(c)" in resp.text
    assert "overlaySignature" in resp.text
    assert "Saved counts" in resp.text


def test_health_says_whether_saved_work_survives_a_deploy():
    """The default SQLite file is on the container filesystem, which the
    host replaces on every deploy. The only other way to learn that is to
    lose the work."""
    body = client.get("/api/health").json()
    assert "storage_durable" in body
    if not body["storage_durable"]:
        assert "deploy" in body["storage_note"]


def test_live_state_reports_fills_and_the_recorded_record(tmp_path, monkeypatch):
    _seed_store(tmp_path, monkeypatch)
    from t3_engine.ai_advisor import trade_journal
    monkeypatch.setattr(trade_journal, "_factory", None)
    engine, _ = _live_engine_for("FILLSUSDT", monkeypatch)
    engine.fills.append({"event": "TP_HIT", "label": "TP1", "price": 6.19, "timeframe": "5m",
                         "position_id": "p1", "wave_label": "3", "side": "LONG",
                         "quantity": 1.0, "position_realized_pnl": 4.0, "at": 1,
                         "closed": False})
    body = client.get("/api/live/state",
                      params={"symbol": "FILLSUSDT", "timeframe": "5m"}).json()
    assert body["fills"][0]["label"] == "TP1"
    assert "take_profits_hit" in body["trade_record"]


def test_index_shows_take_profit_legs_that_filled():
    """A position with two of four legs filled used to read as
    "open positions: 1, closed trades: 0" and nothing else."""
    resp = client.get("/")
    assert "Take-profit legs filled" in resp.text
    assert "fillsTableHtml" in resp.text
    assert "Realized (open trades)" in resp.text


def test_live_fibonacci_and_subwaves_are_derived_from_the_ai_count(tmp_path, monkeypatch):
    """A finer degree drawn from a different reading than the labels above
    it is not extra detail, it is a contradiction on the same candles."""
    store = _seed_store(tmp_path, monkeypatch)
    engine, candles = _live_engine_for("DERIVEUSDT", monkeypatch)

    def leg(label, a, b):
        return {"label": label, "start_time": candles[a].open_time // 1000,
                "end_time": candles[b].open_time // 1000,
                "start_price": candles[a].close, "end_price": candles[b].close,
                "direction": "UP" if candles[b].close >= candles[a].close else "DOWN"}

    store.save("bybit", "DERIVEUSDT", "5m", candles[-1].open_time, len(candles), {
        "accepted": [{"structure": "IMPULSE", "waves": [
            leg("1", 0, 20), leg("2", 20, 30), leg("3", 30, 60), leg("4", 60, 70)]}],
        "projection": {"next_label": "5",
                       "targets": [{"ratio": 1.0, "price": candles[-1].close * 1.2,
                                    "primary": True}]},
        "coverage": {}, "summary": "impulse up",
    }, model="m")

    body = client.get("/api/live/state",
                      params={"symbol": "DERIVEUSDT", "timeframe": "5m"}).json()
    # The grid is projected for the wave the AGENT says comes next.
    assert body["fibonacci_levels"], "the AI count should produce a Fibonacci projection"
    # ...for the wave now FORMING (5), not the one after it. Asking for the
    # grid of the wave after a developing 5 means wave A, which has no
    # formula in this codebase and came back empty.
    assert all(level["for_wave"] == "5" for level in body["fibonacci_levels"])
    # ...and the subwave detail sits under the agent's own motive waves.
    for sub in body["subwave_history"]:
        assert any(w["start_time"] <= sub["start_time"] <= w["end_time"]
                   for w in body["scenarios"][0]["waves"])


def test_multi_timeframe_on_synthetic_analyses_four_different_charts():
    """The defect this fixes, end to end: /api/run on the synthetic source
    returned the same 5m series for every timeframe, so a multi-timeframe
    pass reconciled one chart with itself. The model noticed before anyone
    else and wrote "the supplied data repeats 5m" into its verdict."""
    seen = {}
    for tf in ("5m", "15m", "1h", "4h"):
        body = client.get("/api/run", params={"source": "synthetic", "cycles": 2,
                                              "timeframe": tf, "threshold": 60}).json()
        assert body["timeframe"] == tf, "the requested timeframe must be the one analysed"
        seen[tf] = tuple(round(c["close"], 4) for c in body["candles"][:20])
        # bars really are that long
        gap = body["candles"][1]["time"] - body["candles"][0]["time"]
        assert gap == {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}[tf]
    assert len(set(seen.values())) == 4, "four timeframes, four different charts"


# ---- the Claude tab: a second reading, held beside the first -----------

def test_claude_chart_aggregates_stored_candles_up_to_the_asked_timeframe(tmp_path, monkeypatch):
    """The count in that tab was made on exactly these bars, so it has to
    be drawn on exactly these bars - not on a freshly fetched window that
    has since moved."""
    from t3_engine.backtest.synthetic_data import generate_synthetic_series
    from t3_engine.database import candle_store
    _seed_store(tmp_path, monkeypatch)
    monkeypatch.setattr(candle_store, "_factory", None)
    candle_store.save("INJUSDT", "15m", generate_synthetic_series(num_cycles=4))

    body = client.get("/api/claude/chart",
                      params={"symbol": "INJUSDT", "timeframe": "1h"}).json()
    assert body["timeframe"] == "1h"
    assert body["candles"], "1h bars should be built from the stored 15m series"
    gap = body["candles"][1]["time"] - body["candles"][0]["time"]
    assert gap == 3600
    assert "aggregated from the stored 15m" in body["note"]


def test_claude_chart_refuses_to_invent_a_finer_timeframe(tmp_path, monkeypatch):
    """5m does not divide out of 15m. Returning something plausible would
    be inventing bars that never traded."""
    from t3_engine.backtest.synthetic_data import generate_synthetic_series
    from t3_engine.database import candle_store
    _seed_store(tmp_path, monkeypatch)
    monkeypatch.setattr(candle_store, "_factory", None)
    candle_store.save("INJUSDT", "15m", generate_synthetic_series(num_cycles=2))

    body = client.get("/api/claude/chart",
                      params={"symbol": "INJUSDT", "timeframe": "5m"}).json()
    assert body["candles"] == []
    assert "cannot be divided out of" in body["note"]


def test_claude_chart_says_what_to_do_when_nothing_is_stored(tmp_path, monkeypatch):
    from t3_engine.database import candle_store
    _seed_store(tmp_path, monkeypatch)
    monkeypatch.setattr(candle_store, "_factory", None)
    body = client.get("/api/claude/chart",
                      params={"symbol": "NOSUCHUSDT", "timeframe": "4h"}).json()
    assert body["candles"] == []
    assert "press build" in body["note"].lower() and "costs nothing" in body["note"]


def test_claude_counts_are_stored_apart_from_the_analysts(tmp_path, monkeypatch):
    """Two readings of one instrument are only useful if you can hold them
    side by side; a shared key means the newer silently replaces the
    older."""
    store = _seed_store(tmp_path, monkeypatch)
    store.save("bybit", "INJUSDT", "4h", 1000, 500,
               {"accepted": [{"structure": "IMPULSE", "waves": []}], "summary": "analyst"}, model="m")
    store.save(server_module.CLAUDE_SOURCE, "INJUSDT", "4h", 1000, 500,
               {"accepted": [{"structure": "ZIGZAG", "waves": []}], "summary": "second opinion"},
               model="claude")

    mine = client.get("/api/claude/chart",
                      params={"symbol": "INJUSDT", "timeframe": "4h"}).json()
    assert mine["summary"] == "second opinion"
    theirs = client.get("/api/ai/saved", params={"source": "bybit", "symbol": "INJUSDT"}).json()
    assert theirs["timeframes"][0]["summary"] == "analyst"


def test_claude_timeframes_lists_only_what_has_a_count(tmp_path, monkeypatch):
    store = _seed_store(tmp_path, monkeypatch)
    for tf in ("4h", "1h"):
        store.save(server_module.CLAUDE_SOURCE, "INJUSDT", tf, 1000, 500,
                   {"accepted": [{"structure": "IMPULSE", "waves": []}],
                    "coverage": {"covered_fraction": 0.8}, "summary": f"{tf} read"}, model="claude")

    body = client.get("/api/claude/timeframes", params={"symbol": "INJUSDT"}).json()
    assert [r["timeframe"] for r in body["timeframes"]] == ["1h", "4h"]   # shortest first
    assert all(r["structures"] == 1 for r in body["timeframes"])
    # Every timeframe a build covers is advertised even before it has a
    # count, so the tab can show which degrees are still missing rather
    # than pretending they do not exist.
    assert body["known_timeframes"] == list(server_module.CLAUDE_TIMEFRAMES)


def test_index_has_a_claude_tab_with_timeframe_switching():
    resp = client.get("/")
    assert 'data-tab="claude"' in resp.text
    assert "tab-claude" in resp.text
    assert "drawClaudeTimeframe" in resp.text
    assert "data-claude-tf" in resp.text


def test_claude_build_fetches_files_and_counts_a_whole_history(tmp_path, monkeypatch):
    """The end-to-end shape of a Build: real bars in, candles filed, a
    rule-checked count stored - and not one model call anywhere in it."""
    from t3_engine.backtest.synthetic_data import generate_synthetic_series_for
    from t3_engine.database import candle_store

    store = _seed_store(tmp_path, monkeypatch)
    monkeypatch.setattr(candle_store, "_factory", None)

    fetched = {}

    def fake_load(source, symbol, timeframe, limit, cycles):
        tf = Timeframe(timeframe)
        fetched[timeframe] = limit
        assert source == "bybit", "a Build reads the exchange, never the demo fixture"
        return generate_synthetic_series_for(tf, num_cycles=9), symbol, tf

    monkeypatch.setattr(server_module, "load_candles", fake_load)

    count = server_module.build_claude_timeframe("INJUSDT", "1h", 1500)
    assert fetched == {"1h": 1500}
    assert count["candles_analysed"] > 500
    assert count["accepted"], "nothing was counted"
    assert count["summary"]

    # the bars themselves survived, not just the count
    assert len(candle_store.load("INJUSDT", "1h")) == count["candles_analysed"]
    stored = store.load(server_module.CLAUDE_SOURCE, "INJUSDT", "1h")
    assert stored is not None and stored.payload["accepted"]

    body = client.get("/api/claude/chart",
                      params={"symbol": "INJUSDT", "timeframe": "1h"}).json()
    assert len(body["candles"]) == count["candles_analysed"]
    assert body["accepted"] and body["pivots"]
    assert body["deviation_pct"] == count["deviation_pct"]


def test_claude_build_rejects_timeframes_it_cannot_serve():
    resp = client.post("/api/claude/build", json={"symbol": "INJUSDT", "timeframes": ["3d"]})
    assert resp.status_code == 400
    assert "No usable timeframes" in resp.json()["detail"]


def test_index_claude_tab_offers_a_build_and_draws_subwaves():
    """The tab the user was shown drew wave lines over an empty chart. A
    Build button and a subwave pass are what that was missing."""
    text = client.get("/").text
    assert 'id="claudeBuild"' in text
    assert "drawClaudeSubwaves" in text
    assert "renderClaudeForecast" in text
    assert "renderClaudeContext" in text


def test_warm_up_skips_a_symbol_whose_count_is_still_fresh(tmp_path, monkeypatch):
    """A deploy replaces the container and the first person to open the tab
    should not be the one who discovers it is empty. Restarts come in
    bursts, though, so a count made in the last hour is left alone."""
    import time as _time

    store = _seed_store(tmp_path, monkeypatch)
    now = _time.time()
    for tf in server_module.CLAUDE_TIMEFRAMES:
        store.save(server_module.CLAUDE_SOURCE, "INJUSDT", tf, 1000, 1500,
                   {"accepted": [], "summary": ""}, model="engine-rules")
    assert server_module.stale_timeframes("INJUSDT", now=now) == []
    # ...and an hour later every one of them is worth redoing.
    assert (server_module.stale_timeframes(
        "INJUSDT", now=now + server_module.WARMUP_MAX_AGE_SECONDS + 1)
        == list(server_module.CLAUDE_TIMEFRAMES))


def test_warm_up_reads_its_symbols_from_the_environment(monkeypatch):
    monkeypatch.delenv(server_module.WARMUP_SYMBOLS_ENV, raising=False)
    assert server_module.warmup_symbols() == []
    monkeypatch.setenv(server_module.WARMUP_SYMBOLS_ENV, "injusdt, BTCUSDT ,")
    assert server_module.warmup_symbols() == ["INJUSDT", "BTCUSDT"]


def test_a_rebuild_keeps_the_reading_written_beside_the_count(tmp_path, monkeypatch):
    """The count is derived - rerun it and you get it back. The prose
    beside it is not, and the warm-up runs on every restart."""
    from t3_engine.backtest.synthetic_data import generate_synthetic_series_for
    from t3_engine.database import candle_store

    store = _seed_store(tmp_path, monkeypatch)
    monkeypatch.setattr(candle_store, "_factory", None)
    monkeypatch.setattr(server_module, "load_candles",
                        lambda source, symbol, timeframe, limit, cycles: (
                            generate_synthetic_series_for(Timeframe(timeframe), num_cycles=9),
                            symbol, Timeframe(timeframe)))

    server_module.build_claude_timeframe("INJUSDT", "1h", 1500)
    stored = store.load(server_module.CLAUDE_SOURCE, "INJUSDT", "1h")
    payload = dict(stored.payload)
    payload["reading"] = "Wave 1 down from 6.714; above 6.224 this reading is finished."
    store.save(server_module.CLAUDE_SOURCE, "INJUSDT", "1h", stored.last_candle_time,
               stored.candle_count, payload, model=stored.model)

    server_module.build_claude_timeframe("INJUSDT", "1h", 1500)
    again = store.load(server_module.CLAUDE_SOURCE, "INJUSDT", "1h")
    assert again.payload["reading"] == payload["reading"]
    assert again.payload["accepted"], "and the count itself was still rebuilt"
