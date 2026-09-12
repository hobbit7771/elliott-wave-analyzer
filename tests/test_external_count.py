"""The external-count validator is a trust boundary: everything a
language model proposes crosses it before reaching a chart or a trade.
These tests are written from the attacker's side - each one is a shape of
nonsense an LLM realistically returns, and the requirement is that it
never gets through."""

import pytest

from t3_engine.common.models import Pivot
from t3_engine.common.types import Direction, Timeframe, WaveLabel
from t3_engine.elliott_engine.external_count import (
    ExternalCountRejected,
    parse_proposed_legs,
    validate_external_count,
)


def pivot(i, price, kind, confirmed_at=None):
    return Pivot(index=i, timestamp=i * 1000, price=price, kind=kind,
                 confirmed_at_index=i if confirmed_at is None else confirmed_at)


def valid_impulse_pivots():
    """A clean, rule-valid 1-2-3-4-5 (wave 3 longest, wave 4 clear of
    wave 1, wave 2 holds above the start)."""
    return [
        pivot(0, 100, "LOW"),
        pivot(1, 150, "HIGH"),
        pivot(2, 120, "LOW"),
        pivot(3, 250, "HIGH"),
        pivot(4, 200, "LOW"),
        pivot(5, 280, "HIGH"),
    ]


def legs(*pairs):
    labels = ["1", "2", "3", "4", "5", "A", "B", "C"]
    return [{"label": labels[i], "start_pivot_index": a, "end_pivot_index": b}
            for i, (a, b) in enumerate(pairs)]


def test_accepts_a_well_formed_valid_impulse():
    result = validate_external_count(valid_impulse_pivots(), legs((0, 1), (1, 2), (2, 3), (3, 4), (4, 5)),
                                      Direction.UP, Timeframe.M5)
    assert result.valid
    assert [w.label for w in result.waves] == [WaveLabel.W1, WaveLabel.W2, WaveLabel.W3,
                                                WaveLabel.W4, WaveLabel.W5]
    # Prices come from the SERVER's pivots, never from the proposal.
    assert result.waves[0].start_price == 100
    assert result.waves[-1].end_price == 280


def test_rejects_pivot_index_that_does_not_exist():
    """The single most common LLM failure: confidently citing index 42 of
    a 6-element list."""
    with pytest.raises(ExternalCountRejected, match="does not exist"):
        validate_external_count(valid_impulse_pivots(), legs((0, 1), (1, 2), (2, 3), (3, 4), (4, 99)),
                                 Direction.UP, Timeframe.M5)


def test_rejects_wave_running_backwards_in_time():
    with pytest.raises(ExternalCountRejected, match="forward in time"):
        validate_external_count(valid_impulse_pivots(), legs((0, 1), (1, 2), (3, 2), (3, 4), (4, 5)),
                                 Direction.UP, Timeframe.M5)


def test_rejects_disjoint_legs_that_do_not_form_one_structure():
    """Wave 3 starting somewhere other than where wave 2 ended is not a
    count, it's two unrelated fragments."""
    with pytest.raises(ExternalCountRejected, match="one continuous structure"):
        validate_external_count(valid_impulse_pivots(), legs((0, 1), (1, 2), (3, 4), (4, 5)),
                                 Direction.UP, Timeframe.M5)


def test_rejects_out_of_order_or_invented_labels():
    proposal = [
        {"label": "3", "start_pivot_index": 0, "end_pivot_index": 1},
        {"label": "1", "start_pivot_index": 1, "end_pivot_index": 2},
    ]
    with pytest.raises(ExternalCountRejected, match="canonical order"):
        parse_proposed_legs(proposal)

    with pytest.raises(ExternalCountRejected, match="canonical order"):
        parse_proposed_legs([{"label": "Z", "start_pivot_index": 0, "end_pivot_index": 1}])


def test_rejects_abc_without_a_full_motive_in_front_of_it():
    """A-B-C is position 6-7-8 of the canonical order. Sending it as the
    first three waves means labelling a correction with no impulse under
    it, which the canonical-order check catches before any rule runs."""
    proposal = [
        {"label": "A", "start_pivot_index": 0, "end_pivot_index": 1},
        {"label": "B", "start_pivot_index": 1, "end_pivot_index": 2},
        {"label": "C", "start_pivot_index": 2, "end_pivot_index": 3},
    ]
    with pytest.raises(ExternalCountRejected, match="canonical order"):
        parse_proposed_legs(proposal)


def test_rejects_non_numeric_and_fractional_indices():
    with pytest.raises(ExternalCountRejected, match="must be a number"):
        parse_proposed_legs([{"label": "1", "start_pivot_index": "first", "end_pivot_index": 1}])
    with pytest.raises(ExternalCountRejected, match="whole pivot index"):
        parse_proposed_legs([{"label": "1", "start_pivot_index": 0, "end_pivot_index": 1.5}])


def test_rejects_a_leg_between_two_swings_of_the_same_kind():
    """LOW -> LOW is not a wave; it's two lows with something in between
    that the count is pretending isn't there."""
    pivots = valid_impulse_pivots()
    with pytest.raises(ExternalCountRejected, match="swing high to a swing low"):
        validate_external_count(pivots, legs((0, 2), (2, 3), (3, 4), (4, 5)),
                                 Direction.UP, Timeframe.M5)


def test_rejects_an_up_count_that_starts_on_a_high():
    pivots = valid_impulse_pivots()
    with pytest.raises(ExternalCountRejected, match="must start at a LOW swing"):
        validate_external_count(pivots, legs((1, 2), (2, 3), (3, 4), (4, 5)),
                                 Direction.UP, Timeframe.M5)


def test_rejects_pivots_not_yet_confirmed_at_the_cutoff_candle():
    """The no-lookahead guarantee has to survive the external path too: a
    model looking at the whole history must not be able to build a count
    out of swings that had not been confirmed yet at the decision point."""
    pivots = valid_impulse_pivots()
    with pytest.raises(ExternalCountRejected, match="had not happened yet"):
        validate_external_count(pivots, legs((0, 1), (1, 2), (2, 3), (3, 4), (4, 5)),
                                 Direction.UP, Timeframe.M5, max_confirmed_index=3)


def test_well_formed_but_rule_breaking_count_is_reported_not_raised():
    """A count can be structurally sane (real pivots, right order,
    alternating) and still be an illegal impulse. That's a different
    answer from malformed input: it comes back as a result carrying the
    broken rule, so the caller can show WHY the model's count is wrong."""
    pivots = [
        pivot(0, 100, "LOW"),
        pivot(1, 200, "HIGH"),   # wave1, length 100
        pivot(2, 180, "LOW"),
        pivot(3, 210, "HIGH"),   # wave3, length 30 - the shortest of 1/3/5
        pivot(4, 205, "LOW"),
        pivot(5, 400, "HIGH"),   # wave5, length 195
    ]
    result = validate_external_count(pivots, legs((0, 1), (1, 2), (2, 3), (3, 4), (4, 5)),
                                      Direction.UP, Timeframe.M5)
    assert not result.valid
    assert result.broken_rule == "WAVE3_SHORTEST"
    assert "Wave 3" in result.notes


def test_wave4_overlap_is_rejected_as_an_impulse_and_not_quietly_relabelled():
    """Same rule the internal engine follows: the external path gets no
    diagonal amnesty either."""
    pivots = [
        pivot(0, 100, "LOW"), pivot(1, 140, "HIGH"), pivot(2, 110, "LOW"),
        pivot(3, 130, "HIGH"), pivot(4, 115, "LOW"), pivot(5, 125, "HIGH"),
    ]
    result = validate_external_count(pivots, legs((0, 1), (1, 2), (2, 3), (3, 4), (4, 5)),
                                      Direction.UP, Timeframe.M5)
    assert not result.valid
    assert result.broken_rule == "WAVE4_OVERLAPS_WAVE1"


def test_rejects_empty_and_oversized_proposals():
    with pytest.raises(ExternalCountRejected, match="non-empty list"):
        parse_proposed_legs([])
    with pytest.raises(ExternalCountRejected, match="not a list|non-empty list"):
        parse_proposed_legs({"label": "1"})

    # 9 legs: one more than the 8 labels a single count can ever have.
    oversized = [{"label": "1", "start_pivot_index": i, "end_pivot_index": i + 1} for i in range(9)]
    with pytest.raises(ExternalCountRejected, match="at most 8"):
        parse_proposed_legs(oversized)
