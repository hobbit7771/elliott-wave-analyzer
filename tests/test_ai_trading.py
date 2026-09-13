"""Trading the agent's count with the engine's own rules.

A live session shows the AI's read of the market and nothing else, so the
paper trading on that chart comes from the same place. What must NOT
change is the trust boundary: the model names waves, the server computes
every price.
"""

import pytest

from t3_engine.ai_advisor.ai_trading import (
    analysis_fingerprint,
    scenario_from_analysis,
)
from t3_engine.common.types import Direction, Timeframe, WaveLabel, WaveStatus


def _wave(label, start_time, end_time, start_price, end_price):
    return {"label": label, "start_time": start_time, "end_time": end_time,
            "start_price": start_price, "end_price": end_price,
            "direction": "UP" if end_price >= start_price else "DOWN"}


def _impulse_1234(next_label="5", target=25.0):
    return {
        "analysed_at": 1_700_000_000,
        "accepted": [{"structure": "IMPULSE", "waves": [
            _wave("1", 100, 200, 10.0, 15.0),
            _wave("2", 200, 300, 15.0, 12.0),
            _wave("3", 300, 400, 12.0, 22.0),
            _wave("4", 400, 500, 22.0, 18.0),
        ]}],
        "projection": {"next_label": next_label,
                       "targets": [{"ratio": 1.0, "price": target, "primary": True}]},
    }


def test_the_wave_now_forming_is_what_gets_traded():
    """A saved count is a list of FINISHED waves. Nothing is tradeable
    about a finished wave - the trade is in the one now developing, which
    is what the entry plans key off."""
    scenario = scenario_from_analysis(_impulse_1234(), Timeframe.M5)
    assert [w.label.value for w in scenario.waves] == ["1", "2", "3", "4", "5"]
    current = scenario.current_wave
    assert current.label == WaveLabel.W5
    assert current.status == WaveStatus.DEVELOPING
    # It has begun and not finished: anchored at wave 4's end, no length yet.
    assert current.start_price == 18.0 and current.end_price == 18.0
    assert current.start_timestamp == current.end_timestamp == 500_000
    # ...and it points where the projection says price is going.
    assert current.direction == Direction.UP


def test_the_developing_wave_follows_the_projection_not_the_last_direction():
    down = _impulse_1234(target=14.0)     # projection says lower, not higher
    scenario = scenario_from_analysis(down, Timeframe.M5)
    assert scenario.current_wave.direction == Direction.DOWN


def test_without_a_target_the_next_wave_alternates():
    payload = _impulse_1234()
    payload["projection"] = {"next_label": "5"}
    scenario = scenario_from_analysis(payload, Timeframe.M5)
    # wave 4 went down, so wave 5 goes up
    assert scenario.current_wave.direction == Direction.UP


def test_times_are_converted_from_chart_seconds_to_engine_milliseconds():
    """The saved payload is in seconds (it is fed to the charting library);
    everything inside the engine is milliseconds. Getting this wrong puts
    every AI wave in 1970."""
    scenario = scenario_from_analysis(_impulse_1234(), Timeframe.M5)
    assert scenario.waves[0].start_timestamp == 100_000
    assert scenario.waves[0].end_timestamp == 200_000


def test_scoring_reuses_the_engines_own_formulas():
    """An AI scenario and an engine scenario must be on ONE scale, or a
    confidence threshold means two different things."""
    from t3_engine.elliott_engine.scenario import ScenarioEngine, score_fibonacci
    scenario = scenario_from_analysis(_impulse_1234(), Timeframe.M5)
    assert scenario.elliott_validity == min(1.0, 0.5 + 0.1 * len(scenario.waves))
    assert scenario.fib_score == score_fibonacci(scenario.waves)
    assert scenario.invalidation == ScenarioEngine._invalidation_level(
        scenario.waves, scenario.current_wave.direction)
    # wave 1's start is the level that kills this count
    assert scenario.invalidation == 10.0


def test_no_scenario_when_there_is_nothing_to_trade():
    assert scenario_from_analysis({}, Timeframe.M5) is None
    assert scenario_from_analysis({"accepted": []}, Timeframe.M5) is None
    # a structure whose legs no entry plan covers is drawn, never traded -
    # offering one would mean inventing a rule this project does not have
    triangle = {"accepted": [{"structure": "TRIANGLE", "waves": [
        _wave("A", 100, 200, 10.0, 15.0)]}]}
    assert scenario_from_analysis(triangle, Timeframe.M5) is None


def test_a_finished_count_has_nothing_developing():
    """Wave C ends a correction: there is no next label, so there is no
    open wave and nothing to trade."""
    done = {"accepted": [{"structure": "ZIGZAG", "waves": [
        _wave("A", 100, 200, 20.0, 15.0),
        _wave("B", 200, 300, 15.0, 18.0),
        _wave("C", 300, 400, 18.0, 12.0)]}],
        "projection": None}
    assert scenario_from_analysis(done, Timeframe.M5) is None


def test_the_newest_structure_is_the_one_traded():
    two = _impulse_1234()
    two["accepted"] = [
        {"structure": "ZIGZAG", "waves": [_wave("A", 10, 20, 5.0, 8.0),
                                          _wave("B", 20, 30, 8.0, 6.0),
                                          _wave("C", 30, 40, 6.0, 9.0)]},
        two["accepted"][0],
    ]
    scenario = scenario_from_analysis(two, Timeframe.M5)
    assert [w.label.value for w in scenario.waves] == ["1", "2", "3", "4", "5"]


def test_a_malformed_wave_is_skipped_not_crashed_on():
    payload = _impulse_1234()
    payload["accepted"][0]["waves"].append({"label": "nonsense"})
    scenario = scenario_from_analysis(payload, Timeframe.M5)
    assert [w.label.value for w in scenario.waves] == ["1", "2", "3", "4", "5"]


def test_the_fingerprint_changes_only_when_the_count_does():
    """A live chart is polled every few seconds. Re-installing an
    unchanged count on every poll would re-evaluate the same entry over
    and over."""
    a = analysis_fingerprint(_impulse_1234())
    assert a == analysis_fingerprint(_impulse_1234())
    assert a != analysis_fingerprint(_impulse_1234(next_label="A"))
    changed = _impulse_1234()
    changed["analysed_at"] = 1_700_009_999
    assert a != analysis_fingerprint(changed)
    assert analysis_fingerprint(None) == ""


# ---- live derives EVERYTHING from the agent's count ---------------------

def test_the_fibonacci_grid_is_projected_for_the_wave_now_forming():
    """Two conventions meet here. A tradeable scenario carries the
    DEVELOPING wave in `waves`; the Fibonacci helper expects completed
    waves plus the projected one named separately. Handing it the trading
    shape asked for the grid of the wave AFTER the one forming - wave A
    after a developing 5 - which has no formula and came back empty."""
    from t3_engine.ai_advisor.ai_trading import grid_scenario
    from t3_engine.dashboard.server import fibonacci_levels_for_scenario

    scenario = scenario_from_analysis(_impulse_1234(), Timeframe.M5)
    grid = grid_scenario(scenario)
    assert [w.label.value for w in grid.waves] == ["1", "2", "3", "4"]
    assert grid.next_expected_label == WaveLabel.W5
    levels = fibonacci_levels_for_scenario(grid)
    assert levels and all(lvl["for_wave"] == "5" for lvl in levels)


def test_no_grid_scenario_without_both_a_past_and_a_present():
    from t3_engine.ai_advisor.ai_trading import grid_scenario
    assert grid_scenario(None) is None


def test_subwaves_are_built_under_the_agents_own_motive_waves():
    """The finer degree has to come from the same reading as the labels
    above it, or the two contradict each other on the same candles."""
    from t3_engine.ai_advisor.ai_trading import subwaves_for
    from t3_engine.backtest.synthetic_data import generate_synthetic_series
    candles = generate_synthetic_series(num_cycles=2)

    def leg(label, a, b):
        return _wave(label, candles[a].open_time // 1000, candles[b].open_time // 1000,
                     candles[a].close, candles[b].close)

    analysis = {"accepted": [{"structure": "IMPULSE", "waves": [
        leg("1", 0, 25), leg("2", 25, 35), leg("3", 35, 70), leg("4", 70, 80)]}],
        "projection": {"next_label": "5"}}
    scenario = scenario_from_analysis(analysis, Timeframe.M5)
    subs = subwaves_for(scenario, candles)

    assert subs, "a motive wave spanning 25 candles should subdivide"
    motive = [w for w in scenario.waves if w.label.value in ("1", "3", "5")]
    for sub in subs:
        assert any(w.start_timestamp <= sub.start_timestamp <= w.end_timestamp for w in motive)
    # The developing wave has no end yet, so there is nothing inside it to
    # count - it must never be subdivided.
    developing = scenario.current_wave
    assert not any(sub.start_timestamp >= developing.start_timestamp
                   and sub.end_timestamp > developing.start_timestamp for sub in subs)


def test_no_candles_means_no_subwaves_rather_than_an_error():
    from t3_engine.ai_advisor.ai_trading import subwaves_for
    scenario = scenario_from_analysis(_impulse_1234(), Timeframe.M5)
    assert subwaves_for(scenario, []) == []
    assert subwaves_for(None, [1, 2, 3]) == []
