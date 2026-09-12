from t3_engine.common.models import Wave
from t3_engine.common.types import Direction, StructureType, Timeframe, WaveLabel, WaveStatus
from t3_engine.elliott_engine.rules_correction import classify_abc, classify_correction, classify_triangle, is_combination
from t3_engine.elliott_engine.rules_diagonal import validate_diagonal
from t3_engine.elliott_engine.rules_impulse import (
    check_wave2_rule,
    check_wave3_not_shortest,
    check_wave4_overlap,
    validate_impulse,
)


def w(label, start, end, high=None, low=None):
    return Wave(
        wave_id=f"w-{label}", parent_wave_id=None, degree=Timeframe.M5, label=label,
        direction=Direction.UP if end >= start else Direction.DOWN,
        start_timestamp=0, end_timestamp=1, start_price=start, end_price=end,
        high=high if high is not None else max(start, end), low=low if low is not None else min(start, end),
    )


# ---- Wave 2 rule ----

def test_wave2_cannot_retrace_past_wave1_start_up():
    wave1 = w(WaveLabel.W1, 100, 200)
    wave2_ok = w(WaveLabel.W2, 200, 120)
    wave2_bad = w(WaveLabel.W2, 200, 90)
    assert check_wave2_rule(wave1, wave2_ok, Direction.UP).valid
    assert not check_wave2_rule(wave1, wave2_bad, Direction.UP).valid


def test_wave2_rule_down_direction():
    wave1 = w(WaveLabel.W1, 200, 100)
    wave2_ok = w(WaveLabel.W2, 100, 160)
    wave2_bad = w(WaveLabel.W2, 100, 210)
    assert check_wave2_rule(wave1, wave2_ok, Direction.DOWN).valid
    assert not check_wave2_rule(wave1, wave2_bad, Direction.DOWN).valid


# ---- Wave 3 not shortest ----

def test_wave3_shortest_is_invalid():
    wave1 = w(WaveLabel.W1, 100, 150)  # len 50
    wave3 = w(WaveLabel.W3, 120, 140)  # len 20 <- shortest, invalid
    wave5 = w(WaveLabel.W5, 130, 200)  # len 70
    result = check_wave3_not_shortest(wave1, wave3, wave5)
    assert not result.valid
    assert result.broken_rule == "WAVE3_SHORTEST"


def test_wave3_not_shortest_pending_without_wave5():
    wave1 = w(WaveLabel.W1, 100, 150)
    wave3 = w(WaveLabel.W3, 120, 300)
    assert check_wave3_not_shortest(wave1, wave3, None).valid


# ---- Wave 4 overlap ----

def test_wave4_overlap_invalidates_regular_impulse():
    wave1 = w(WaveLabel.W1, 100, 150)  # high=150
    wave4_bad = w(WaveLabel.W4, 200, 140)  # low=140 < wave1 high -> overlap
    wave4_ok = w(WaveLabel.W4, 200, 160)
    assert not check_wave4_overlap(wave1, wave4_bad, Direction.UP).valid
    assert check_wave4_overlap(wave1, wave4_ok, Direction.UP).valid


def test_wave4_overlap_allowed_for_diagonal():
    wave1 = w(WaveLabel.W1, 100, 150)
    wave4_bad = w(WaveLabel.W4, 200, 140)
    assert check_wave4_overlap(wave1, wave4_bad, Direction.UP, allow_overlap=True).valid


def test_full_impulse_validation_passes_clean_case():
    wave1 = w(WaveLabel.W1, 100, 150)
    wave2 = w(WaveLabel.W2, 150, 120)
    wave3 = w(WaveLabel.W3, 120, 250)
    wave4 = w(WaveLabel.W4, 250, 200)
    wave5 = w(WaveLabel.W5, 200, 280)
    result = validate_impulse(wave1, wave2, wave3, wave4, wave5, Direction.UP)
    assert result.valid


def test_full_impulse_validation_fails_on_wave4_overlap():
    wave1 = w(WaveLabel.W1, 100, 150)
    wave2 = w(WaveLabel.W2, 150, 120)
    wave3 = w(WaveLabel.W3, 120, 250)
    wave4 = w(WaveLabel.W4, 250, 140)  # overlaps wave1 high (150)
    wave5 = w(WaveLabel.W5, 140, 280)
    result = validate_impulse(wave1, wave2, wave3, wave4, wave5, Direction.UP)
    assert not result.valid
    assert result.broken_rule == "WAVE4_OVERLAPS_WAVE1"


# ---- Diagonal ----

def test_diagonal_valid_when_contracting_and_wave2_rule_holds():
    wave1 = w(WaveLabel.W1, 100, 150)   # len 50
    wave2 = w(WaveLabel.W2, 150, 120)
    wave3 = w(WaveLabel.W3, 120, 155)   # len 35 < 50
    wave4 = w(WaveLabel.W4, 155, 130)   # overlaps wave1 (allowed)
    wave5 = w(WaveLabel.W5, 130, 150)   # len 20 < 35
    result = validate_diagonal(wave1, wave2, wave3, wave4, wave5, Direction.UP, variant="CONTRACTING")
    assert result.valid


def test_diagonal_rejected_when_not_contracting():
    wave1 = w(WaveLabel.W1, 100, 150)   # len 50
    wave2 = w(WaveLabel.W2, 150, 120)
    wave3 = w(WaveLabel.W3, 120, 250)   # len 130 > 50, not contracting
    wave4 = w(WaveLabel.W4, 250, 130)
    wave5 = w(WaveLabel.W5, 130, 170)
    result = validate_diagonal(wave1, wave2, wave3, wave4, wave5, Direction.UP, variant="CONTRACTING")
    assert not result.valid
    assert result.broken_rule == "DIAGONAL_NOT_CONTRACTING"


# ---- Corrections ----

def test_classify_zigzag_shallow_b_retrace():
    wave_a = w(WaveLabel.A, 200, 100)   # down, len 100
    wave_b = w(WaveLabel.B, 100, 145)   # retraces 45% of A
    wave_c = w(WaveLabel.C, 145, 20)
    assert classify_abc(wave_a, wave_b, wave_c) == StructureType.ZIGZAG


def test_classify_flat_deep_b_retrace():
    wave_a = w(WaveLabel.A, 200, 100)   # len 100
    wave_b = w(WaveLabel.B, 100, 195)   # retraces ~95% of A
    wave_c = w(WaveLabel.C, 195, 90)    # len ~105, roughly == A
    assert classify_abc(wave_a, wave_b, wave_c) == StructureType.FLAT


def test_classify_triangle_contracting_legs():
    legs = [
        w("A", 100, 150),  # 50
        w("B", 150, 115),  # 35
        w("C", 115, 140),  # 25
        w("D", 140, 122),  # 18
        w("E", 122, 132),  # 10
    ]
    assert classify_triangle(legs) == StructureType.TRIANGLE


def test_classify_triangle_rejects_non_monotonic_legs():
    legs = [
        w("A", 100, 150),
        w("B", 150, 115),
        w("C", 115, 160),  # expands then...
        w("D", 160, 100),  # ...contracts - not monotonic
        w("E", 100, 130),
    ]
    assert classify_triangle(legs) is None


def test_unknown_correction_fallback_not_forced():
    wave_a = w(WaveLabel.A, 200, 100)
    wave_b = w(WaveLabel.B, 100, 250)  # B makes a new extreme beyond A start - neither clean zigzag nor flat
    wave_c = w(WaveLabel.C, 250, 240)
    assert classify_abc(wave_a, wave_b, wave_c) == StructureType.UNKNOWN_CORRECTION
