"""Corrective structures proposed from outside: zigzag, flat, triangle.

The motive path (tests/test_external_count.py) already proves an external
count can't invent pivots or break the impulse rules. These tests cover the
other half, added for the AI analyst: a correction is NOT an impulse with
letters on it, and the three simple corrective patterns each have a
defining constraint that decides what the pattern MEANS. A "zigzag" whose
C fails to pass A puts its target in a different place than the real
thing, so mislabelling one is not a cosmetic error.
"""

import pytest

from t3_engine.common.models import Pivot
from t3_engine.common.types import Timeframe
from t3_engine.elliott_engine.external_count import (
    STRUCTURE_FLAT,
    STRUCTURE_TRIANGLE,
    STRUCTURE_ZIGZAG,
    ExternalCountRejected,
    validate_external_structure,
)

DEGREE = Timeframe.M5


def pivots(*prices):
    """Alternating HIGH/LOW pivots at the given prices, starting with
    whichever kind the first two prices imply."""
    out = []
    for i, price in enumerate(prices):
        if i == 0:
            kind = "HIGH" if prices[1] < price else "LOW"
        else:
            kind = "LOW" if prices[i - 1] > price else "HIGH"
        out.append(Pivot(index=i * 10, timestamp=i * 60_000, price=float(price),
                         kind=kind, confirmed_at_index=i * 10 + 1))
    return out


def legs(labels, count):
    return [{"label": label, "start_pivot_index": i, "end_pivot_index": i + 1}
            for i, label in enumerate(labels[:count])]


ABC = ["A", "B", "C"]
ABCDE = ["A", "B", "C", "D", "E"]


# ---- zigzag ----

def test_a_textbook_zigzag_is_accepted():
    # Down from 100 to 70 (A), back up to 88 (B), down to 60 (C beyond A).
    result = validate_external_structure(pivots(100, 70, 88, 60), legs(ABC, 3),
                                         STRUCTURE_ZIGZAG, DEGREE)
    assert result.valid
    assert [w.label.value for w in result.waves] == ABC
    assert result.waves[0].direction.value == "DOWN"   # read off wave A, not imposed
    assert result.waves[1].direction.value == "UP"     # B goes the other way


def test_zigzag_wave_b_may_not_pass_the_start_of_wave_a():
    """The one hard rule of a zigzag's B wave. A B that retraces more than
    all of A means the whole structure started somewhere else."""
    result = validate_external_structure(pivots(100, 70, 105, 60), legs(ABC, 3),
                                         STRUCTURE_ZIGZAG, DEGREE)
    assert not result.valid
    assert result.broken_rule == "ZIGZAG_B_PASSES_START_OF_A"
    assert "105" in result.notes


def test_zigzag_wave_c_must_carry_beyond_the_end_of_wave_a():
    """C stopping short of A is a flat or an unfinished pattern - calling
    it a zigzag would project the target from the wrong measurement."""
    result = validate_external_structure(pivots(100, 70, 88, 75), legs(ABC, 3),
                                         STRUCTURE_ZIGZAG, DEGREE)
    assert not result.valid
    assert result.broken_rule == "ZIGZAG_C_FAILS_TO_PASS_A"


def test_an_upward_zigzag_is_judged_by_the_same_rules_mirrored():
    result = validate_external_structure(pivots(60, 90, 72, 100), legs(ABC, 3),
                                         STRUCTURE_ZIGZAG, DEGREE)
    assert result.valid
    assert result.waves[0].direction.value == "UP"


# ---- flat ----

def test_a_flat_needs_a_deep_b_wave():
    """What separates a flat from a zigzag is the DEPTH of B, so a shallow
    B labelled as a flat is rejected with that named explicitly."""
    # A: 100 -> 70 (30 points). B back to 78 = 26.7% retrace. Far too shallow.
    result = validate_external_structure(pivots(100, 70, 78, 60), legs(ABC, 3),
                                         STRUCTURE_FLAT, DEGREE)
    assert not result.valid
    assert result.broken_rule == "FLAT_B_TOO_SHALLOW"
    assert "zigzag" in result.notes


def test_an_expanded_flat_is_legal_even_though_b_passes_the_start_of_a():
    """The expanded flat is the MOST COMMON flat - B beyond the start of A
    is its defining feature, not a violation. The zigzag rule must not leak
    into the flat path."""
    result = validate_external_structure(pivots(100, 70, 106, 60), legs(ABC, 3),
                                         STRUCTURE_FLAT, DEGREE)
    assert result.valid


def test_a_running_flat_is_legal_even_though_c_falls_short_of_a():
    result = validate_external_structure(pivots(100, 70, 98, 75), legs(ABC, 3),
                                         STRUCTURE_FLAT, DEGREE)
    assert result.valid


# ---- triangle ----

def test_a_contracting_triangle_is_accepted():
    # Legs 40, 30, 24, 18, 12 - contracting throughout.
    result = validate_external_structure(pivots(140, 100, 130, 106, 124, 112),
                                         legs(ABCDE, 5), STRUCTURE_TRIANGLE, DEGREE)
    assert result.valid
    assert [w.label.value for w in result.waves] == ABCDE


def test_a_triangle_that_neither_contracts_nor_expands_is_not_a_triangle():
    """Five overlapping legs that wander are just five legs. The thrust
    measurement that makes triangles worth identifying needs a real
    apex."""
    result = validate_external_structure(pivots(140, 100, 160, 106, 124, 112),
                                         legs(ABCDE, 5), STRUCTURE_TRIANGLE, DEGREE)
    assert not result.valid
    assert result.broken_rule == "TRIANGLE_NOT_MONOTONE"


def test_an_expanding_triangle_is_accepted_as_the_rare_variant():
    # Legs 10, 14, 20, 28, 40 - expanding throughout.
    result = validate_external_structure(pivots(100, 90, 104, 84, 112, 72),
                                         legs(ABCDE, 5), STRUCTURE_TRIANGLE, DEGREE)
    assert result.valid


def test_a_triangle_must_have_all_five_legs():
    with pytest.raises(ExternalCountRejected, match="5 legs"):
        validate_external_structure(pivots(140, 100, 130, 106), legs(ABCDE, 3),
                                    STRUCTURE_TRIANGLE, DEGREE)


# ---- shared guards still apply ----

def test_a_correction_cannot_be_submitted_with_impulse_labels():
    """An A-B-C is not a 1-2-3 with different letters - it is validated by
    entirely different rules, so the label set is enforced per structure."""
    bad = [{"label": "1", "start_pivot_index": 0, "end_pivot_index": 1},
           {"label": "2", "start_pivot_index": 1, "end_pivot_index": 2},
           {"label": "3", "start_pivot_index": 2, "end_pivot_index": 3}]
    with pytest.raises(ExternalCountRejected, match="canonical order"):
        validate_external_structure(pivots(100, 70, 88, 60), bad, STRUCTURE_ZIGZAG, DEGREE)


def test_a_correction_with_a_hallucinated_pivot_is_rejected_before_any_rule_check():
    bad = [{"label": "A", "start_pivot_index": 0, "end_pivot_index": 1},
           {"label": "B", "start_pivot_index": 1, "end_pivot_index": 2},
           {"label": "C", "start_pivot_index": 2, "end_pivot_index": 99}]
    with pytest.raises(ExternalCountRejected, match="does not exist"):
        validate_external_structure(pivots(100, 70, 88, 60), bad, STRUCTURE_ZIGZAG, DEGREE)


def test_a_correction_may_not_reference_a_pivot_confirmed_after_the_cutoff():
    """Same no-lookahead guarantee as the motive path: the corrective
    branch must not be a way around it."""
    with pytest.raises(ExternalCountRejected, match="had not happened yet"):
        validate_external_structure(pivots(100, 70, 88, 60), legs(ABC, 3),
                                    STRUCTURE_ZIGZAG, DEGREE, max_confirmed_index=5)


def test_an_unknown_structure_name_is_refused():
    with pytest.raises(ExternalCountRejected, match="Unknown structure"):
        validate_external_structure(pivots(100, 70, 88, 60), legs(ABC, 3), "HEAD_AND_SHOULDERS", DEGREE)
