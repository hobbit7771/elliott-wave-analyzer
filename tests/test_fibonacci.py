import math

from t3_engine.fibonacci.calculator import (
    nearest_ratio_score,
    retracement_levels,
    wave2_levels,
    wave3_targets_from_wave2_end,
    wave4_levels,
    wave5_targets,
    wave_c_targets,
)


def test_wave2_retracement_of_up_move():
    levels = wave2_levels(100, 200)  # wave1 up 100->200
    by_ratio = {round(l.ratio, 3): l.price for l in levels}
    assert math.isclose(by_ratio[0.5], 150)
    assert math.isclose(by_ratio[0.618], 200 - 100 * 0.618)


def test_wave3_targets_extend_from_wave2_end_not_wave1_end():
    # wave1: 100 -> 200 (length 100); wave2 ends at 160
    levels = wave3_targets_from_wave2_end(100, 200, wave2_end=160)
    by_ratio = {round(l.ratio, 3): l.price for l in levels}
    assert math.isclose(by_ratio[1.0], 260)
    assert math.isclose(by_ratio[1.618], 160 + 161.8)


def test_wave4_retracement_of_wave3():
    levels = wave4_levels(200, 400)  # wave3 up 200->400, length 200
    by_ratio = {round(l.ratio, 3): l.price for l in levels}
    assert math.isclose(by_ratio[0.382], 400 - 200 * 0.382)


def test_wave5_targets_relative_to_wave1_length():
    levels = wave5_targets(wave1_start=100, wave1_end=200, wave4_end=350)
    by_ratio = {round(l.ratio, 3): l.price for l in levels}
    assert math.isclose(by_ratio[1.0], 450)


def test_wave_c_targets_relative_to_wave_a():
    levels = wave_c_targets(wave_a_start=300, wave_a_end=200, wave_b_end=250)
    by_ratio = {round(l.ratio, 3): l.price for l in levels}
    # wave A length = -100 (down move), C projects further down from B end
    assert math.isclose(by_ratio[1.0], 150)


def test_nearest_ratio_score_perfect_hit_is_one():
    levels = retracement_levels(100, 200, [0.618])
    target_price = levels[0].price
    score = nearest_ratio_score(target_price, levels, reference_length=100)
    assert math.isclose(score, 1.0, abs_tol=1e-9)


def test_nearest_ratio_score_far_away_is_zero():
    levels = retracement_levels(100, 200, [0.618])
    score = nearest_ratio_score(50, levels, reference_length=100)
    assert score == 0.0
