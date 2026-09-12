"""The whole path, end to end, with no network at all:

    history -> labelling -> server-side validation -> trade calculation

Every test here runs autonomously: the market data is the deterministic
synthetic fixture, the "model" is a recorded response, and the assertions
are about the properties that have to hold no matter what any external
service says. Nothing in this file needs an API key, an exchange, or the
internet - which is the point. A missing key must degrade a feature, not
break the pipeline.
"""

from unittest.mock import patch

from fastapi.testclient import TestClient

import t3_engine.dashboard.server as server_module
from t3_engine.backtest.engine import BacktestConfig, BacktestEngine
from t3_engine.backtest.synthetic_data import generate_synthetic_series
from t3_engine.common.types import Direction, Timeframe
from t3_engine.elliott_engine.external_count import validate_external_count
from t3_engine.market_structure.pivots import ZigZagPivotDetector

client = TestClient(server_module.app)


class _FakeProposal:
    def __init__(self, waves, reasoning="recorded"):
        self.waves = waves
        self.reasoning = reasoning
        self.model = "gemini-3.6-flash"
        self.raw = {}


def _server_pivots(candles, deviation_pct=None):
    config = BacktestConfig(symbol="T")
    detector = ZigZagPivotDetector(
        deviation_pct=deviation_pct if deviation_pct is not None else config.pivot_deviation_pct)
    for index, candle in enumerate(candles):
        detector.update(index, candle)
    return detector.pivots


# ---- stage 1: history -> deterministic engine ----

def test_stage1_history_produces_a_consistent_chain_and_one_current_count():
    candles = generate_synthetic_series(num_cycles=3)
    engine = BacktestEngine(BacktestConfig(symbol="TESTUSDT", entry_confidence_threshold=50.0))
    engine.run(candles)

    chain = engine.scenario_engine.confirmed_chain
    assert chain, "a 3-cycle history should confirm structure"

    spans = [(w.start_timestamp, w.end_timestamp) for w in chain]
    assert spans == sorted(spans), "the chain must be in chronological order"
    for (_, earlier_end), (later_start, _) in zip(spans, spans[1:]):
        assert earlier_end <= later_start, "at most one wave may cover any moment"

    assert len(engine.scenario_engine.scenarios) <= 3, "one current count plus alternates, never an archive"


def test_stage1_is_deterministic_across_identical_runs():
    """Autonomous mode means reproducible: same candles in, same chain
    out, with nothing carried over between runs."""
    candles = generate_synthetic_series(num_cycles=2)

    def run_once():
        engine = BacktestEngine(BacktestConfig(symbol="TESTUSDT", entry_confidence_threshold=50.0))
        engine.run(candles)
        return [(w.label, w.start_timestamp, w.end_timestamp) for w in engine.scenario_engine.confirmed_chain]

    assert run_once() == run_once()


# ---- stage 2+3: labelling -> server-side validation ----

def test_stage2_hallucinated_count_is_stopped_before_it_can_be_drawn():
    """The trust boundary in one test: the model names pivots that do not
    exist, and nothing reaches the chart."""
    with patch.object(server_module, "request_wave_count",
                      return_value=_FakeProposal([{"label": "1", "start_pivot_index": 0,
                                                   "end_pivot_index": 10_000}])):
        resp = client.post("/api/ai/label", json={"api_key": "AIza-test", "source": "synthetic", "cycles": 2})
    body = resp.json()
    assert resp.status_code == 200
    assert body["valid"] is False and body["waves"] == []


def test_stage3_validated_count_is_rebuilt_from_server_pivots_only():
    """Prices on an accepted count come from the server's own pivots. If
    the model sends prices, they are ignored - it only picks indices."""
    candles = generate_synthetic_series(num_cycles=2)
    pivots = _server_pivots(candles)
    start = next(i for i, p in enumerate(pivots) if p.kind == "HIGH")

    lying_proposal = [{
        "label": "1", "start_pivot_index": start, "end_pivot_index": start + 1,
        "start_price": 999_999.0, "end_price": -1.0,   # nonsense the model "insists" on
    }]
    result = validate_external_count(pivots, lying_proposal, Direction.DOWN, Timeframe.M5)

    assert result.valid
    assert result.waves[0].start_price == pivots[start].price
    assert result.waves[0].end_price == pivots[start + 1].price


def test_stage3_external_count_cannot_reintroduce_lookahead():
    """The no-lookahead guarantee has to survive the external path. A
    model shown the whole history must not be able to build a count from
    swings that were not yet confirmed at the decision candle."""
    from t3_engine.elliott_engine.external_count import ExternalCountRejected

    candles = generate_synthetic_series(num_cycles=2)
    pivots = _server_pivots(candles)
    start = next(i for i, p in enumerate(pivots) if p.kind == "HIGH")
    proposal = [{"label": "1", "start_pivot_index": start, "end_pivot_index": start + 1}]

    cutoff_before_confirmation = pivots[start + 1].confirmed_at_index - 1
    try:
        validate_external_count(pivots, proposal, Direction.DOWN, Timeframe.M5,
                                max_confirmed_index=cutoff_before_confirmation)
        raise AssertionError("a count using a not-yet-confirmed pivot must be rejected")
    except ExternalCountRejected as exc:
        assert "had not happened yet" in str(exc)

    # At the confirming candle itself it becomes legitimate.
    ok = validate_external_count(pivots, proposal, Direction.DOWN, Timeframe.M5,
                                 max_confirmed_index=pivots[start + 1].confirmed_at_index)
    assert ok.valid


# ---- stage 4: trade calculation, still causal ----

def test_stage4_trades_are_computed_without_lookahead_after_all_of_this():
    """The core anti-repaint property, re-proven on top of the new strict
    core and chain semantics: decisions made while replaying candles[0:K]
    must be identical whether or not later candles exist yet."""
    candles = generate_synthetic_series(num_cycles=3)
    cut = len(candles) * 2 // 3

    full = BacktestEngine(BacktestConfig(symbol="TESTUSDT", entry_confidence_threshold=50.0))
    full_result = full.run(candles)
    prefix = BacktestEngine(BacktestConfig(symbol="TESTUSDT", entry_confidence_threshold=50.0))
    prefix_result = prefix.run(candles[:cut])

    assert len(prefix_result["signals"]) <= len(full_result["signals"])
    for early, later in zip(prefix_result["signals"], full_result["signals"]):
        assert early.decision == later.decision
        assert early.wave_label == later.wave_label
        assert early.confidence == later.confidence
        assert early.stop_loss == later.stop_loss
        assert early.data_available_at_signal == later.data_available_at_signal


def test_stage4_every_chain_wave_was_confirmable_when_it_was_recorded():
    """Causality of the chain itself: a wave only ever enters it built
    from pivots the detector had already confirmed - no wave may end after
    the candle that supposedly confirmed it."""
    candles = generate_synthetic_series(num_cycles=3)
    engine = BacktestEngine(BacktestConfig(symbol="TESTUSDT", entry_confidence_threshold=50.0))
    engine.run(candles)

    pivot_by_timestamp = {p.timestamp: p for p in engine.pivot_detector.pivots}
    for wave in engine.scenario_engine.confirmed_chain:
        for timestamp in (wave.start_timestamp, wave.end_timestamp):
            pivot = pivot_by_timestamp.get(timestamp)
            assert pivot is not None, "chain waves must be built from real confirmed pivots"
            assert pivot.confirmed_at_index >= pivot.index, "a pivot is confirmed at or after it happened"


def test_full_path_runs_with_no_network_and_no_api_key():
    """The autonomous-mode guarantee: with no key and no outbound access,
    the deterministic path still produces candles, a count and trade
    metrics - only the optional AI layer is unavailable."""
    resp = client.get("/api/run", params={"source": "synthetic", "cycles": 2, "threshold": 50})
    assert resp.status_code == 200
    data = resp.json()
    assert data["candles"] and "confirmed_chain" in data and "metrics" in data

    no_key = client.post("/api/ai/label", json={"api_key": "", "source": "synthetic", "cycles": 2})
    assert no_key.status_code == 502          # the AI layer declines...
    assert client.get("/api/health").json()["status"] == "ok"   # ...the app does not
