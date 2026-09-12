import pytest

from t3_engine.common.models import Scenario, Wave, next_id
from t3_engine.common.types import Direction, EntryStage, SignalDecision, Timeframe, WaveLabel, WaveStatus
from t3_engine.signal_engine.entry_timing import EntryTimingTracker, IllegalStageTransition
from t3_engine.signal_engine.scoring import DEFAULT_WEIGHTS, ScoreBreakdown, compute_confidence, evaluate_entry
from t3_engine.signal_engine.targets import plan_wave3_long, plan_wave4_short, plan_wave5_long, plan_wave_c_short


def w(label, start, end):
    return Wave(wave_id=next_id("w"), parent_wave_id=None, degree=Timeframe.M5, label=label,
                direction=Direction.UP if end >= start else Direction.DOWN,
                start_timestamp=0, end_timestamp=1, start_price=start, end_price=end,
                high=max(start, end), low=min(start, end))


def make_scenario(status=WaveStatus.DEVELOPING, probability=80.0):
    return Scenario(scenario_id=next_id("scenario"), degree=Timeframe.M5, status=status, probability=probability)


# ---- entry timing ----

def test_entry_timing_sequential_and_no_skip():
    tracker = EntryTimingTracker()
    tracker.advance(EntryStage.PREDICTION, "wave2 likely ending")
    tracker.advance(EntryStage.SETUP, "in termination zone")
    with pytest.raises(IllegalStageTransition):
        tracker.advance(EntryStage.TRIGGERED, "skip ahead")
    tracker.advance(EntryStage.ARMED, "micro wave1 up formed")
    tracker.advance(EntryStage.TRIGGERED, "BOS confirmed")
    assert tracker.is_actionable


def test_entry_timing_reset():
    tracker = EntryTimingTracker()
    tracker.advance(EntryStage.PREDICTION, "x")
    tracker.reset("invalidated")
    assert tracker.stage == EntryStage.NONE
    assert not tracker.is_actionable


# ---- scoring ----

def test_weights_sum_to_one():
    assert abs(sum(DEFAULT_WEIGHTS.values()) - 1.0) < 1e-9


def test_confidence_bounded_0_100():
    b = ScoreBreakdown(1, 1, 1, 1, 1, 1, 1, 1)
    assert compute_confidence(b) == 100.0
    b0 = ScoreBreakdown(0, 0, 0, 0, 0, 0, 0, 0)
    assert compute_confidence(b0) == 0.0


def test_evaluate_entry_rejects_invalidated_scenario_even_with_perfect_score():
    scenario = make_scenario(status=WaveStatus.INVALIDATED)
    b = ScoreBreakdown(1, 1, 1, 1, 1, 1, 1, 1)
    sig = evaluate_entry(symbol="BTCUSDT", scenario=scenario, wave_label=WaveLabel.W3,
                          entry_stage=EntryStage.TRIGGERED, breakdown=b, entry_zone=(100, 101),
                          stop_loss=95, take_profits=[], risk_reward=3.0, invalidation=95,
                          now_ms=0, data_available_at=0)
    assert sig.decision == SignalDecision.SIGNAL_REJECTED.value
    assert "HARD_ELLIOTT_RULE_VIOLATED" in sig.rejection_reason


def test_evaluate_entry_rejects_a_and_b_waves():
    scenario = make_scenario()
    b = ScoreBreakdown(1, 1, 1, 1, 1, 1, 1, 1)
    sig = evaluate_entry(symbol="BTCUSDT", scenario=scenario, wave_label=WaveLabel.A,
                          entry_stage=EntryStage.TRIGGERED, breakdown=b, entry_zone=(100, 101),
                          stop_loss=95, take_profits=[], risk_reward=3.0, invalidation=95,
                          now_ms=0, data_available_at=0)
    assert sig.decision == SignalDecision.SIGNAL_REJECTED.value
    assert "WAVE_NOT_TRADEABLE" in sig.rejection_reason


def test_evaluate_entry_rejects_low_confidence():
    scenario = make_scenario()
    b = ScoreBreakdown(0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1)
    sig = evaluate_entry(symbol="BTCUSDT", scenario=scenario, wave_label=WaveLabel.W3,
                          entry_stage=EntryStage.TRIGGERED, breakdown=b, entry_zone=(100, 101),
                          stop_loss=95, take_profits=[], risk_reward=3.0, invalidation=95,
                          now_ms=0, data_available_at=0)
    assert sig.decision == SignalDecision.SIGNAL_REJECTED.value
    assert "CONFIDENCE_BELOW_THRESHOLD" in sig.rejection_reason


def test_evaluate_entry_accepts_good_wave3_setup():
    scenario = make_scenario()
    b = ScoreBreakdown(0.9, 0.9, 0.8, 0.8, 0.8, 0.7, 0.7, 0.8)
    sig = evaluate_entry(symbol="BTCUSDT", scenario=scenario, wave_label=WaveLabel.W3,
                          entry_stage=EntryStage.TRIGGERED, breakdown=b, entry_zone=(100, 101),
                          stop_loss=95, take_profits=[], risk_reward=3.0, invalidation=95,
                          now_ms=0, data_available_at=0)
    assert sig.decision == SignalDecision.SIGNAL_ACCEPTED.value


def test_evaluate_entry_rejects_wrong_entry_stage():
    scenario = make_scenario()
    b = ScoreBreakdown(0.9, 0.9, 0.8, 0.8, 0.8, 0.7, 0.7, 0.8)
    sig = evaluate_entry(symbol="BTCUSDT", scenario=scenario, wave_label=WaveLabel.W3,
                          entry_stage=EntryStage.SETUP, breakdown=b, entry_zone=(100, 101),
                          stop_loss=95, take_profits=[], risk_reward=3.0, invalidation=95,
                          now_ms=0, data_available_at=0)
    assert sig.decision == SignalDecision.SIGNAL_REJECTED.value
    assert "ENTRY_STAGE_NOT_ACTIONABLE" in sig.rejection_reason


# ---- targets ----

def test_plan_wave3_long_targets_extend_beyond_wave1():
    wave1 = w(WaveLabel.W1, 100, 200)
    wave2 = w(WaveLabel.W2, 200, 150)
    plan = plan_wave3_long(wave1, wave2, entry_price=155)
    assert plan.stop_loss == 150
    assert all(tp.price > 155 for tp in plan.take_profits)
    assert abs(sum(tp.fraction for tp in plan.take_profits) - 1.0) < 1e-9


def test_plan_wave4_short_targets_between_0_236_and_0_382():
    wave3 = w(WaveLabel.W3, 150, 400)
    plan = plan_wave4_short(wave3, entry_price=390)
    assert plan.take_profits[0].price > plan.take_profits[1].price  # 0.236 retrace above 0.382 retrace


def test_plan_wave5_long_uses_wave1_length():
    wave1 = w(WaveLabel.W1, 100, 200)
    wave4 = w(WaveLabel.W4, 500, 350)
    plan = plan_wave5_long(wave1, wave4, entry_price=360)
    assert plan.stop_loss == 350
    assert plan.take_profits[1].price == 450  # 1.0x wave1 length (100) from wave4 end (350)


def test_plan_wave_c_short_targets_below_b():
    wave_a = w(WaveLabel.A, 300, 200)
    wave_b = w(WaveLabel.B, 200, 250)
    plan = plan_wave_c_short(wave_a, wave_b, entry_price=240)
    assert plan.stop_loss == 250
    assert all(tp.price < 250 for tp in plan.take_profits)
