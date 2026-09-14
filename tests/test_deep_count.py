"""The no-model count of a whole history.

These tests care about one thing above all: that nothing gets ACCEPTED
here that the hard rules would reject anywhere else. A search that places
a structure at every swing is only worth having if the rules are what
stop it.
"""

import pytest

from t3_engine.ai_advisor import deep_count
from t3_engine.backtest.synthetic_data import generate_synthetic_series_for
from t3_engine.common.types import Direction, Timeframe
from t3_engine.elliott_engine.external_count import (
    MOTIVE_STRUCTURES,
    validate_external_structure,
)


@pytest.fixture(scope="module")
def counted():
    candles = generate_synthetic_series_for(Timeframe.M5, num_cycles=17)
    return candles, deep_count.build_count(candles, Timeframe.M5, "SYNTHETIC")


def test_the_whole_series_is_counted_not_a_window_of_it(counted):
    candles, out = counted
    assert out["candles_analysed"] == len(candles) >= 1500
    assert out["first_time"] == candles[0].open_time // 1000
    assert out["last_time"] == candles[-1].open_time // 1000


def test_most_of_the_chart_ends_up_labelled(counted):
    """The failure this replaces: a count covering the left third of the
    chart, with the right two thirds bare and nothing saying so."""
    _, out = counted
    assert out["coverage"]["covered_fraction"] > 0.8, out["coverage"]


def test_every_accepted_structure_re_validates_against_the_hard_rules(counted):
    """The point of the whole module. Each accepted structure is fed back
    through `validate_external_structure` from its own recorded pivot
    indices - the same call that grades a model's proposal - and must come
    back valid. If the search could accept something the rules reject, the
    rules would not be doing any work here."""
    candles, out = counted
    assert out["accepted"], "nothing was counted at all"
    pivots = deep_count.pivots_at(candles, out["deviation_pct"])
    for structure in out["accepted"]:
        indices = structure["pivot_indices"]
        labels = [w["label"] for w in structure["waves"]]
        proposed = [{"label": label, "start_pivot_index": indices[i],
                     "end_pivot_index": indices[i + 1]}
                    for i, label in enumerate(labels)]
        start = pivots[indices[0]]
        direction = Direction.UP if start.kind == "LOW" else Direction.DOWN
        verdict = validate_external_structure(
            pivots, proposed, structure["structure"], Timeframe.M5,
            direction=direction if structure["structure"] in MOTIVE_STRUCTURES else None)
        assert verdict.valid, (structure["structure"], labels, verdict.broken_rule)


def test_structures_do_not_overlap_in_time(counted):
    """Two readings of the same candles, both drawn, is the exact thing
    the confirmed chain exists to prevent - it must not come back through
    this door."""
    _, out = counted
    spans = [(s["waves"][0]["start_time"], s["waves"][-1]["end_time"])
             for s in out["accepted"] if s.get("waves")]
    for (a_start, a_end), (b_start, b_end) in zip(spans, spans[1:]):
        assert b_start >= a_end, f"{a_start}-{a_end} overlaps {b_start}-{b_end}"


def test_every_wave_price_is_a_real_pivot_price(counted):
    """No price in the output may be interpolated, smoothed or invented -
    each one is a swing high or low the detector actually found."""
    candles, out = counted
    pivots = deep_count.pivots_at(candles, out["deviation_pct"])
    prices = {round(p.price, 8) for p in pivots}
    for structure in out["accepted"]:
        for wave in structure["waves"]:
            assert round(wave["start_price"], 8) in prices
            assert round(wave["end_price"], 8) in prices


def test_the_deviation_is_measured_rather_than_assumed(counted):
    """Every rung is tried and the search is reported, so the choice can
    be argued with instead of taken on faith.

    The rule is a coverage FLOOR, then the coarsest deviation that
    reaches it - the largest degree the data supports, which is the order
    Elliott is counted in and also the only reading a chart can show
    without eighty structures on it."""
    _, out = counted
    search = out["deviation_search"]
    assert len(search) == len(deep_count._DEVIATION_LADDER)
    def in_band(a):
        return (a["covered_fraction"] >= deep_count.MIN_USEFUL_COVERAGE
                and deep_count.MIN_STRUCTURES_FOR_A_DEGREE <= a["structures"]
                <= deep_count.MAX_STRUCTURES_FOR_A_DEGREE)

    usable = [a for a in search if in_band(a)]
    assert usable, "no deviation on the ladder accounted for this chart at all"
    chosen = max(usable, key=lambda a: (round(a["mean_score"], 1), a["deviation_pct"]))
    assert out["deviation_pct"] == chosen["deviation_pct"]
    assert out["coverage"]["covered_fraction"] >= deep_count.MIN_USEFUL_COVERAGE
    # ...and nothing coarser managed it, or that one would have won.
    # Anything coarser was either out of the band, or the rules recognised
    # materially less of what was there.
    coarser = [a for a in search if a["deviation_pct"] > out["deviation_pct"]]
    assert all(not in_band(a) or round(a["mean_score"], 1) < round(chosen["mean_score"], 1)
               for a in coarser)
    assert (deep_count.MIN_STRUCTURES_FOR_A_DEGREE
            <= len(out["accepted"]) - (1 if out["accepted"][-1].get("partial") else 0)
            <= deep_count.MAX_STRUCTURES_FOR_A_DEGREE)


def test_a_short_history_is_refused_rather_than_counted():
    candles = generate_synthetic_series_for(Timeframe.M5, num_cycles=1)[:20]
    out = deep_count.build_count(candles, Timeframe.M5, "SHORT")
    assert out["accepted"] == []
    assert "too short" in out["error"]


def test_the_tail_is_fitted_as_a_partial_count_so_a_forecast_exists():
    """A finished 1-2-3-4-5 is history. What is tradeable is the count
    that has reached wave 3 and is building wave 4, so the leftover pivots
    at the right edge are fitted as an incomplete motive count and the
    wave now forming is named from it."""
    candles = generate_synthetic_series_for(Timeframe.H1, num_cycles=7)
    out = deep_count.build_count(candles, Timeframe.H1, "SYNTHETIC")
    partial = [s for s in out["accepted"] if s.get("partial")]
    assert partial, "the tail was left uncounted, so this chart forecasts nothing"
    assert len(partial) == 1
    assert partial[-1] is out["accepted"][-1], "the partial count must be the newest one"
    assert len(partial[0]["waves"]) < 5
    assert out["projection"] and out["projection"]["next_label"]


def test_subwaves_belong_to_motive_waves_and_sit_inside_them(counted):
    """A subwave outside its parent's span is a label that describes some
    other stretch of the chart."""
    _, out = counted
    spans = {}
    for structure in out["accepted"]:
        for wave in structure["waves"]:
            spans[(wave["label"], wave["start_time"])] = (wave["start_time"], wave["end_time"])
    for sub in out["subwaves"]:
        assert sub["parent_label"] in ("1", "3", "5")
        start, end = spans[(sub["parent_label"], sub["parent_start_time"])]
        assert start <= sub["start_time"] <= end
        assert start <= sub["end_time"] <= end


def test_an_invalidation_level_comes_back_with_the_count(counted):
    _, out = counted
    assert out["invalidation"] is not None


def test_a_legal_motive_reading_outranks_a_legal_correction_of_the_same_swings():
    """The search picks between LEGAL readings, and that choice has to be
    principled. A stretch of chart often admits both a five-wave motive
    count and a three-leg corrective one; taking whichever was tested
    first is not a reason, and an earlier version of this module labelled
    a 20% advance a FLAT because the flat test happened to pass.

    The weights are arranged so proportion can never bridge the gap: the
    best conceivable correction scores below the worst conceivable
    motive count."""
    best_correction = (max(deep_count._STRUCTURE_WEIGHT[k]
                           for k in ("ZIGZAG", "TRIANGLE", "FLAT"))
                       + deep_count._FIB_WEIGHT + deep_count._FLATNESS_WEIGHT)
    worst_motive = min(deep_count._STRUCTURE_WEIGHT[k]
                       for k in ("IMPULSE", "DIAGONAL_CONTRACTING", "DIAGONAL_EXPANDING"))
    assert best_correction < worst_motive


def test_a_flat_that_goes_nowhere_outranks_a_flat_that_travels():
    """A flat is the SIDEWAYS correction, and the hard rule only checks
    that wave B is deep. Two flats that are both legal are separated here
    by whether they actually went sideways."""
    from t3_engine.common.models import Wave, next_id
    from t3_engine.common.types import WaveLabel, WaveStatus
    from t3_engine.elliott_engine.external_count import ValidatedExternalCount

    def flat(prices):
        waves = []
        for label, (start, end) in zip((WaveLabel.A, WaveLabel.B, WaveLabel.C), prices):
            waves.append(Wave(
                wave_id=next_id("t"), parent_wave_id=None, degree=Timeframe.H1, label=label,
                direction=Direction.UP if end >= start else Direction.DOWN,
                start_timestamp=0, end_timestamp=1, start_price=start, end_price=end,
                high=max(start, end), low=min(start, end), status=WaveStatus.CONFIRMED))
        return ValidatedExternalCount(waves=waves, structure="FLAT")

    sideways = flat([(100.0, 90.0), (90.0, 99.0), (99.0, 100.5)])
    travelling = flat([(100.0, 90.0), (90.0, 130.0), (130.0, 125.0)])
    assert (deep_count._score_candidate("FLAT", sideways)
            > deep_count._score_candidate("FLAT", travelling))


def test_the_readings_that_lost_are_recorded_beside_the_one_that_won(counted):
    """A preference nobody can see is indistinguishable from a rule. Where
    more than one reading of the same swings was legal, the losers travel
    with the winner so the choice can be argued with."""
    _, out = counted
    contested = [s for s in out["accepted"] if s.get("alternatives")]
    assert contested, "no stretch of this chart admitted a second legal reading at all"
    for structure in contested:
        for alternative in structure["alternatives"]:
            assert alternative["structure"] != structure["structure"]
            assert alternative["score"] <= structure["score"]
