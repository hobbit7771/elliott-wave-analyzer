from t3_engine.common.models import Pivot
from t3_engine.common.types import Direction, Timeframe, WaveStatus
from t3_engine.elliott_engine.scenario import ScenarioEngine, build_candidate_waves


def pivot(i, price, kind):
    return Pivot(index=i, timestamp=i * 1000, price=price, kind=kind, confirmed_at_index=i)


def test_build_candidate_waves_valid_impulse():
    pivots = [
        pivot(0, 100, "LOW"),
        pivot(1, 150, "HIGH"),
        pivot(2, 120, "LOW"),
        pivot(3, 250, "HIGH"),
        pivot(4, 200, "LOW"),
        pivot(5, 280, "HIGH"),
    ]
    result = build_candidate_waves(pivots, Direction.UP, Timeframe.M5)
    assert result["broken_rule"] is None
    assert len(result["waves"]) == 5
    labels = [w.label.value for w in result["waves"]]
    assert labels == ["1", "2", "3", "4", "5"]


def test_build_candidate_waves_invalidated_on_wave4_overlap():
    pivots = [
        pivot(0, 100, "LOW"),
        pivot(1, 150, "HIGH"),
        pivot(2, 120, "LOW"),
        pivot(3, 250, "HIGH"),
        pivot(4, 130, "LOW"),  # overlaps wave1 high (150)
    ]
    result = build_candidate_waves(pivots, Direction.UP, Timeframe.M5)
    assert result["broken_rule"] is not None
    assert result["waves"][-1].status == WaveStatus.INVALIDATED


def test_scenario_engine_rebuild_produces_top3_or_fewer():
    engine = ScenarioEngine(degree=Timeframe.M5)
    pivots = [
        pivot(0, 90, "LOW"),
        pivot(1, 95, "HIGH"),
        pivot(2, 80, "LOW"),
        pivot(3, 150, "HIGH"),
        pivot(4, 120, "LOW"),
        pivot(5, 250, "HIGH"),
        pivot(6, 200, "LOW"),
        pivot(7, 280, "HIGH"),
    ]
    scenarios = engine.rebuild(pivots, Direction.UP)
    assert len(scenarios) <= 3
    assert all(s.probability >= 0 for s in scenarios)
    if len(scenarios) > 1:
        assert scenarios[0].probability >= scenarios[1].probability


def test_scenario_engine_invalidated_scenarios_get_zero_probability_and_excluded():
    engine = ScenarioEngine(degree=Timeframe.M5)
    pivots = [
        pivot(0, 100, "LOW"),
        pivot(1, 150, "HIGH"),
        pivot(2, 120, "LOW"),
        pivot(3, 250, "HIGH"),
        pivot(4, 130, "LOW"),  # invalidates via wave4 overlap
    ]
    scenarios = engine.rebuild(pivots, Direction.UP)
    for s in scenarios:
        assert s.status != WaveStatus.INVALIDATED
        assert s.probability > 0
