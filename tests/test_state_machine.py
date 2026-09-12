import pytest

from t3_engine.elliott_engine.state_machine import ElliottStateMachine, IllegalStateTransition, WaveState


def test_sequential_bullish_path_is_allowed():
    sm = ElliottStateMachine()
    path = [
        WaveState.W1_DEVELOPING, WaveState.W1_CONFIRMED,
        WaveState.W2_DEVELOPING, WaveState.W2_TERMINATION_ZONE, WaveState.W2_CONFIRMED,
        WaveState.W3_TRIGGER_ARMED, WaveState.W3_ACTIVE, WaveState.W3_EXTENDING,
        WaveState.W3_COMPLETING, WaveState.W4_DEVELOPING, WaveState.W4_SHORT_ARMED,
        WaveState.W4_ACTIVE, WaveState.W4_COMPLETING, WaveState.W5_TRIGGER_ARMED,
        WaveState.W5_ACTIVE, WaveState.W5_EXHAUSTION, WaveState.ABC_PENDING,
        WaveState.A_ACTIVE, WaveState.B_ACTIVE, WaveState.C_SHORT_ARMED,
        WaveState.C_ACTIVE, WaveState.C_COMPLETING,
    ]
    for state in path:
        assert sm.transition(state)
    assert sm.state == WaveState.C_COMPLETING


def test_cannot_skip_states():
    sm = ElliottStateMachine()
    with pytest.raises(IllegalStateTransition):
        sm.transition(WaveState.W3_ACTIVE)  # skipping W1/W2 entirely


def test_w3_extending_can_loop():
    sm = ElliottStateMachine(initial=WaveState.W3_EXTENDING)
    assert sm.transition(WaveState.W3_EXTENDING)


def test_invalidate_returns_to_searching_from_any_state():
    sm = ElliottStateMachine(initial=WaveState.W4_ACTIVE)
    sm.invalidate("wave4 overlapped wave1")
    assert sm.state == WaveState.SEARCHING_W1


def test_cycle_restarts_after_c_completing():
    sm = ElliottStateMachine(initial=WaveState.C_COMPLETING)
    assert sm.transition(WaveState.SEARCHING_W1)
