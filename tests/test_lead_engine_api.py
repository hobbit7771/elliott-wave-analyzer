"""The HTTP surface, the feature flag, the replay, and the isolation the
whole module exists for.

The flag tests matter most. "LEAD_ENGINE_ENABLED=false switches it off"
is easy to write and easy to get subtly wrong - a thread that starts
anyway, a route that 500s, an import that opens a socket - so what "off"
means is pinned here: no thread, no socket, no polling, and every route
answering 200 with enabled:false rather than 404.
"""

import time

import pytest
from fastapi.testclient import TestClient

import t3_engine.dashboard.server as server_module
from t3_engine.lead_engine import config as le_config
from t3_engine.lead_engine import engine as engine_module
from t3_engine.lead_engine.config import LeadEngineConfig
from t3_engine.lead_engine.engine import LeadEngine

client = TestClient(server_module.app)

ROUTES = [
    "/api/lead-engine/status",
    "/api/lead-engine/symbols",
    "/api/lead-engine/state/INJUSDT",
    "/api/lead-engine/signals/INJUSDT",
    "/api/lead-engine/metrics/INJUSDT",
    "/api/lead-engine/pressure/INJUSDT",
    "/api/lead-engine/history/INJUSDT",
]


@pytest.fixture(autouse=True)
def _clean_engine():
    engine_module.reset_engine()
    yield
    engine_module.reset_engine()


@pytest.fixture
def disabled(monkeypatch):
    monkeypatch.delenv(le_config.ENABLED_ENV, raising=False)
    monkeypatch.delenv(le_config.ENABLED_ENV_PREFIXED, raising=False)


@pytest.fixture
def switched_on(monkeypatch):
    monkeypatch.setenv(le_config.ENABLED_ENV, "true")


# ---- the flag -----------------------------------------------------------

@pytest.mark.parametrize("path", ROUTES)
def test_every_route_answers_when_the_engine_is_off(path, disabled):
    """200 with enabled:false, never a 404. A 404 is indistinguishable
    from a deploy that shipped without the module, and the tab has to be
    able to tell 'not built' from 'built, switched off'."""
    response = client.get(path)
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert le_config.ENABLED_ENV in str(body)


def test_nothing_starts_while_the_flag_is_off(disabled):
    engine = LeadEngine(LeadEngineConfig.from_env())
    assert engine.config.enabled is False
    assert engine.start() is False
    assert engine.stream is None, "no socket object is even constructed"
    assert engine.oi_poller is None, "no polling thread"
    assert engine.running() is False


def test_the_flag_is_read_at_call_time_not_frozen_at_import(monkeypatch):
    monkeypatch.delenv(le_config.ENABLED_ENV, raising=False)
    monkeypatch.delenv(le_config.ENABLED_ENV_PREFIXED, raising=False)
    assert le_config.enabled() is False
    monkeypatch.setenv(le_config.ENABLED_ENV, "true")
    assert le_config.enabled() is True
    monkeypatch.setenv(le_config.ENABLED_ENV, "no")
    assert le_config.enabled() is False


def test_the_prefixed_spelling_of_the_flag_works_too(monkeypatch):
    monkeypatch.delenv(le_config.ENABLED_ENV, raising=False)
    monkeypatch.setenv(le_config.ENABLED_ENV_PREFIXED, "true")
    assert le_config.enabled() is True


def test_the_btc_reference_is_always_subscribed(monkeypatch):
    """Not a preference: 'did BTC move first' is unanswerable without a
    continuous BTC stream, so it is added whatever the symbol list says."""
    monkeypatch.setenv("T3_LEAD_ENGINE_SYMBOLS", "injusdt,dogeusdt")
    assert le_config.symbols()[0] == le_config.BTC_SYMBOL


# ---- with the engine on -------------------------------------------------

def _fed_engine():
    """An engine driven entirely through handle_message - the same entry
    point the live socket uses - so no network is involved."""
    config = LeadEngineConfig(enabled=True, symbols=["BTCUSDT", "INJUSDT"])
    engine = LeadEngine(config)
    now = int(time.time() * 1000) - 60_000
    bids = [[str(round(5.70 - i * 0.001, 4)), "100"] for i in range(50)]
    asks = [[str(round(5.702 + i * 0.001, 4)), "60"] for i in range(50)]
    engine.handle_message("orderbook.50.INJUSDT", {
        "topic": "orderbook.50.INJUSDT", "type": "snapshot", "ts": now,
        "data": {"u": 1, "b": bids, "a": asks}})
    for i in range(200):
        stamp = now + i * 200
        engine.handle_message("publicTrade.INJUSDT", {
            "topic": "publicTrade.INJUSDT", "ts": stamp,
            "data": [{"T": stamp, "S": "Buy" if i % 3 else "Sell",
                      "v": "5", "p": str(round(5.70 + i * 0.0001, 5))}]})
    engine.handle_message("tickers.INJUSDT", {
        "topic": "tickers.INJUSDT", "ts": now + 60_000,
        "data": {"lastPrice": "5.72"}})
    state = engine.states["INJUSDT"]
    fresh = int(time.time() * 1000)
    state.health.last_book_ms = fresh
    state.health.last_trade_ms = fresh
    state.health.last_ticker_ms = fresh
    return engine


def test_the_facade_returns_the_blocks_the_tab_renders():
    engine = _fed_engine()
    frame = engine.get_state("INJUSDT", force=True)
    for block in ("orderbook", "microprice", "trade_flow", "cvd", "liquidations",
                  "open_interest", "btc_lead", "smc", "elliott", "pressure",
                  "prebreak", "signal", "health"):
        assert block in frame, f"{block} missing from the state frame"
    assert frame["orderbook"]["synced"] is True
    assert set(frame["orderbook"]["obi"]) == {"obi1", "obi5", "obi10", "obi25", "obi50"}
    assert frame["health"]["status"] == "OK"


def test_get_pressure_and_get_signal_agree_with_get_state():
    engine = _fed_engine()
    frame = engine.get_state("INJUSDT", force=True)
    pressure = engine.get_pressure("INJUSDT")
    signal = engine.get_signal("INJUSDT")
    assert pressure["long_pressure"] == frame["pressure"]["long_pressure"]
    assert signal["current"]["state"] == frame["signal"]["state"]


def test_an_unknown_symbol_is_reported_rather_than_invented():
    engine = _fed_engine()
    assert engine.get_state("NOSUCHUSDT")["tracked"] is False


def test_status_lists_every_symbol_with_its_own_health():
    engine = _fed_engine()
    status = engine.status()
    symbols = {row["symbol"] for row in status["symbols"]}
    assert symbols == {"BTCUSDT", "INJUSDT"}
    btc = next(r for r in status["symbols"] if r["symbol"] == "BTCUSDT")
    assert btc["health"] == "DEGRADED", "a symbol with no book of its own says so"


# ---- failure isolation --------------------------------------------------

def test_a_handler_that_throws_does_not_take_down_the_stream():
    from t3_engine.lead_engine.bybit_ws import BybitLeadStream

    def boom(topic, message):
        raise RuntimeError("handler blew up")

    stream = BybitLeadStream(["INJUSDT"], ["1"], boom, "wss://example.invalid")
    stream.feed({"topic": "publicTrade.INJUSDT", "ts": 1, "data": []})
    assert stream.stats.messages == 1, "the frame was counted and the loop survived"


def test_a_lead_engine_that_cannot_start_leaves_the_app_running(monkeypatch):
    """The isolation requirement, exercised rather than asserted."""
    config = LeadEngineConfig(enabled=True, symbols=["INJUSDT"])
    engine = LeadEngine(config)

    def explode(*args, **kwargs):
        raise RuntimeError("no socket for you")

    monkeypatch.setattr("t3_engine.lead_engine.engine.BybitLeadStream", explode)
    assert engine.start() is False
    assert "no socket for you" in engine.start_error
    # ...and the dashboard is untouched.
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/run?source=synthetic&cycles=1").status_code == 200


def test_the_dashboards_own_endpoints_are_unchanged_by_the_new_tab():
    for path in ("/api/health", "/api/ai/config", "/api/claude/timeframes?symbol=INJUSDT"):
        assert client.get(path).status_code == 200


# ---- replay -------------------------------------------------------------

def _capture(break_at=180, steps=260):
    """A support tested repeatedly and then broken, as Bybit frames."""
    import random

    rnd = random.Random(8)
    now = 1_700_000_000_000
    support, tick = 5.70, 0.001
    messages = []

    def sides(price, bid_size, ask_size):
        return ([[str(round(price - tick * (i + 1), 5)), str(max(1, int(bid_size)))]
                 for i in range(20)],
                [[str(round(price + tick * (i + 1), 5)), str(max(1, int(ask_size)))]
                 for i in range(20)])

    price = 5.76
    bids, asks = sides(price, 800, 800)
    messages.append({"topic": "orderbook.50.INJUSDT", "type": "snapshot", "ts": now,
                     "data": {"u": 1, "b": bids, "a": asks}})
    previous = ({b[0] for b in bids}, {a[0] for a in asks})
    update = 1
    for i in range(steps):
        stamp = now + i * 3_000
        if i < break_at:
            price = support + 0.05 * (1 - i / float(break_at)) + rnd.gauss(0, 0.0015)
        else:
            price = support - 0.0015 * (i - break_at)
        price = round(price, 5)
        messages.append({"topic": "publicTrade.INJUSDT", "ts": stamp, "data": [{
            "T": stamp, "S": "Sell" if rnd.random() < 0.7 else "Buy",
            "v": str(round(rnd.uniform(2, 30), 3)), "p": str(price)}]})
        bids, asks = sides(price, 800 * max(0.2, 1 - i / float(steps)),
                           800 * (1 + i / float(steps)))
        live = ({b[0] for b in bids}, {a[0] for a in asks})
        removals = ([[lv, "0"] for lv in previous[0] - live[0]],
                    [[lv, "0"] for lv in previous[1] - live[1]])
        previous = live
        update += 1
        messages.append({"topic": "orderbook.50.INJUSDT", "type": "delta", "ts": stamp,
                         "data": {"u": update, "b": bids + removals[0],
                                  "a": asks + removals[1]}})
    return messages


def test_a_replay_runs_the_same_code_path_as_the_live_engine():
    from t3_engine.lead_engine.replay import run_replay

    report = run_replay("INJUSDT", _capture(), break_pct=0.003, horizon_ms=15 * 60_000)
    assert report["events"] > 400
    assert report["frames"] > 100
    assert report["states_seen"], "the machine produced states"
    assert "DATA_FAILURE" not in report["states_seen"], \
        "a replay must not be degraded merely because the capture is old"


def test_outcomes_are_scored_only_from_prices_after_the_signal():
    """The no-lookahead boundary. Moving the FUTURE must change the score;
    moving the PAST must not."""
    from t3_engine.lead_engine.replay import Call, Replay

    replay = Replay("INJUSDT", break_pct=0.003)
    replay.prices = [(1_000, 10.0), (2_000, 10.0), (3_000, 10.0),
                     (4_000, 9.0), (5_000, 8.0)]
    call = Call(symbol="INJUSDT", state="PRE_BREAK_SHORT", direction="short",
                timestamp_ms=3_000, price=10.0, level=10.0, probability=70.0)
    replay._outcome(call)
    assert call.resolved is True and call.mfe > 0.15

    # Rewrite everything BEFORE the signal: the verdict cannot change.
    same = Call(symbol="INJUSDT", state="PRE_BREAK_SHORT", direction="short",
                timestamp_ms=3_000, price=10.0, level=10.0, probability=70.0)
    replay.prices = [(1_000, 99.0), (2_000, 0.5), (3_000, 10.0),
                     (4_000, 9.0), (5_000, 8.0)]
    replay._outcome(same)
    assert same.resolved is True and same.mfe == pytest.approx(call.mfe)

    # Rewrite the FUTURE: it must.
    other = Call(symbol="INJUSDT", state="PRE_BREAK_SHORT", direction="short",
                 timestamp_ms=3_000, price=10.0, level=10.0, probability=70.0)
    replay.prices = [(1_000, 10.0), (2_000, 10.0), (3_000, 10.0),
                     (4_000, 10.1), (5_000, 10.2)]
    replay._outcome(other)
    assert other.resolved is False


def test_a_price_at_the_signals_own_timestamp_is_not_treated_as_the_future():
    from t3_engine.lead_engine.replay import Replay

    replay = Replay("INJUSDT")
    replay.prices = [(3_000, 1.0), (3_001, 2.0)]
    assert replay._future(3_000) == [(3_001, 2.0)]


def test_a_replay_never_touches_the_live_engine(switched_on):
    from t3_engine.lead_engine.replay import Replay

    live = engine_module.get_engine()
    replay = Replay("INJUSDT")
    assert replay.engine is not live
    assert replay.engine.states is not live.states


def test_the_replay_endpoint_refuses_an_empty_capture(switched_on):
    response = client.post("/api/lead-engine/replay/run",
                           json={"symbol": "INJUSDT", "messages": []})
    assert response.status_code == 400


def test_the_replay_endpoint_scores_a_capture(switched_on):
    response = client.post("/api/lead-engine/replay/run",
                           json={"symbol": "INJUSDT", "messages": _capture(steps=120),
                                 "break_pct": 0.003, "horizon_minutes": 15})
    assert response.status_code == 200
    body = response.json()
    for key in ("precision_pct", "recall_pct", "false_positives",
                "median_lead_seconds", "mfe", "mae", "expected_value"):
        assert key in body, f"{key} missing from the replay report"


# ---- persistence without a browser --------------------------------------

def _engine_with_events():
    import time as _time

    from t3_engine.lead_engine.storage import Storage

    config = LeadEngineConfig(enabled=True, symbols=["INJUSDT"])
    engine = LeadEngine(config, storage=Storage())
    now = int(_time.time() * 1000)
    engine.handle_message("orderbook.50.INJUSDT", {
        "topic": "orderbook.50.INJUSDT", "type": "snapshot", "ts": now,
        "data": {"u": 1, "b": [["5.70", "100"]], "a": [["5.702", "60"]]}})
    for i in range(20):
        stamp = now + i * 100
        engine.handle_message("publicTrade.INJUSDT", {
            "topic": "publicTrade.INJUSDT", "ts": stamp,
            "data": [{"T": stamp, "S": "Sell", "v": "5", "p": "5.70"}]})
    for i in range(3):
        stamp = now + 5_000 + i * 10
        engine.handle_message("allLiquidation.INJUSDT", {
            "topic": "allLiquidation.INJUSDT", "ts": stamp,
            "data": [{"T": stamp, "S": "Sell", "v": "500", "p": "5.70"}]})
    return engine


def test_the_engine_files_what_it_sees_without_a_browser_asking():
    """Before this, nothing was persisted unless someone opened the tab:
    a signal that fired at 03:00 left no trace and the liquidation table
    stayed permanently empty. A monitor has to write on its own clock."""
    from t3_engine.lead_engine.storage import Recorder

    engine = _engine_with_events()
    recorder = Recorder(engine, engine.storage)
    assert recorder.sweep_once() > 0
    buffered = engine.storage.stats()["buffered"]
    assert buffered["lead_engine_features"] == 1
    assert buffered["lead_engine_signals"] == 1
    assert buffered["lead_engine_liquidations"] == 3


def test_a_signal_is_filed_once_per_transition_not_once_per_tick():
    """`changed_at` does not move while a state persists, which is exactly
    the marker that makes this possible."""
    from t3_engine.lead_engine.storage import Recorder

    engine = _engine_with_events()
    recorder = Recorder(engine, engine.storage)
    recorder.sweep_once()
    recorder.sweep_once()
    recorder.sweep_once()
    buffered = engine.storage.stats()["buffered"]
    assert buffered["lead_engine_signals"] == 1, "one transition, one row"
    assert buffered["lead_engine_liquidations"] == 3, "each event filed once"
    assert buffered["lead_engine_features"] == 3, "a feature row per sweep"


def test_the_recorder_never_raises_out_of_a_sweep(monkeypatch):
    from t3_engine.lead_engine.storage import Recorder

    engine = _engine_with_events()
    recorder = Recorder(engine, engine.storage)

    def explode(*args, **kwargs):
        raise RuntimeError("storage is on fire")

    monkeypatch.setattr(engine.storage, "record_features", explode)
    recorder.sweep_once()          # must not raise


def test_starting_the_engine_starts_the_recorder_and_files_a_session():
    config = LeadEngineConfig(enabled=True, symbols=["INJUSDT"])
    engine = LeadEngine(config, connect_fn=lambda url: None,
                        oi_fetcher=lambda s, b: None)
    assert engine.start() is True
    try:
        assert engine.recorder is not None and engine.recorder.running()
        assert engine.storage.stats()["buffered"].get("lead_engine_sessions", 0) >= 1
        assert engine.status()["recorder"]["running"] is True
    finally:
        engine.stop()
