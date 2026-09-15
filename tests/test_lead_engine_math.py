"""The maths rework: normalisation, layers, conflict, calibration.

Each test names the defect it guards. Several of these numbers were
wrong in the first build and the comment says how.
"""

import math
import random

import pytest

from t3_engine.lead_engine import calibration as cal
from t3_engine.lead_engine.candles_rest import ema, ema_series, normalize_interval
from t3_engine.lead_engine.fibonacci import ALL_LEVELS, DrawingStore, levels
from t3_engine.lead_engine.layers import (
    CONFLICT_HIGH,
    CONFLICT_LOW,
    LayerScore,
    detect_conflict,
)
from t3_engine.lead_engine.normalize import (
    MIN_SAMPLES,
    Normalizer,
    RollingFeature,
    agreement,
    normalized_delta,
    split_direction,
)
from t3_engine.lead_engine.orderbook_engine import OrderBook, Wall
from t3_engine.lead_engine.pressure_engine import score as pressure_score


# ---- normalised delta ---------------------------------------------------

def test_normalized_delta_cannot_run_away():
    """The defect: `buy / max(sell, 1e-9)` printed 1,000,000 when nothing
    sold. A feature that can be a million cannot be summed with one that
    lives in -1..+1."""
    assert normalized_delta(0.9, 0.0) == pytest.approx(1.0, abs=1e-6)
    assert normalized_delta(0.0, 0.9) == pytest.approx(-1.0, abs=1e-6)
    assert normalized_delta(5.0, 5.0) == 0.0
    assert normalized_delta(0.0, 0.0) == 0.0, "nothing traded is not an imbalance"
    for buy, sell in [(1e9, 1.0), (1.0, 1e9), (0.0001, 0.0), (7, 3)]:
        assert -1.0 <= normalized_delta(buy, sell) <= 1.0


def test_the_raw_ratio_survives_only_as_a_diagnostic():
    """It is still reported - it is readable and traders know it - but it
    must not reach any score."""
    import inspect

    from t3_engine.lead_engine import layers

    source = inspect.getsource(layers)
    assert "delta_ratio" not in source, \
        "the unbounded ratio must not appear in the scoring layers"


# ---- rolling normalisation ---------------------------------------------

def test_normalisation_is_against_the_instruments_own_history():
    """The real defect: hardcoded full scales. Six basis points of drift
    is enormous on BTCUSDT and noise on DOGEUSDT, so one constant left one
    instrument saturated and the other asleep."""
    quiet = Normalizer("QUIET")
    loud = Normalizer("LOUD")
    rnd = random.Random(11)
    for _ in range(200):
        quiet.update("drift", rnd.gauss(0, 0.5))
        loud.update("drift", rnd.gauss(0, 50.0))
    # The SAME raw value is an outlier on one instrument and ordinary on
    # the other, which is the whole point.
    assert quiet.update("drift", 5.0) > 0.7
    assert abs(loud.update("drift", 5.0)) < 0.2


def test_nothing_is_claimed_before_there_is_history():
    """A normaliser that starts guessing from three samples is how an
    engine fires on its third trade."""
    normalizer = Normalizer("NEW")
    for index in range(MIN_SAMPLES - 1):
        assert normalizer.update("x", index * 10.0) == 0.0
    assert not normalizer.ready("x")


def test_normalisation_never_reads_a_later_sample():
    """Causality. The current value is part of its own reference set - the
    past and the present - but appending FUTURE samples must not change an
    answer already given."""
    first = RollingFeature("x")
    second = RollingFeature("x")
    history = [1.0, 2.0, 1.5, 2.5, 1.2, 2.2, 1.8, 2.8, 1.1]
    for value in history:
        first.observe(value)
        second.observe(value)
    answer = first.zscore(3.0)
    for later in [99.0, -99.0, 50.0]:
        second.observe(later)
    assert first.zscore(3.0) == answer
    assert second.zscore(3.0) != answer, "the later samples did change the LATER answer"


def test_a_percentile_is_robust_to_one_freak_print():
    """Why the one-sided magnitudes use percentiles: a single 40-sigma
    liquidation moves a z-score off the scale and a percentile by one
    sample."""
    feature = RollingFeature("liq")
    for value in range(100):
        feature.observe(float(value))
    before = feature.percentile(50.0)
    feature.observe(1e9)
    assert abs(feature.percentile(50.0) - before) < 0.02


# ---- book alignment -----------------------------------------------------

def _book(bids, asks):
    book = OrderBook("TESTUSDT", 50)
    book.apply({"type": "snapshot", "ts": 1000, "data": {"u": 1, "b": bids, "a": asks}})
    return book


def test_obi1_alone_cannot_create_a_strong_signal():
    """The brief's case: OBI1 +0.98 while the deep book leans the other
    way. That is one queue at the touch, not accumulation."""
    bids = [["10.00", "8000"]] + [[str(round(10.00 - i * 0.01, 4)), "20"] for i in range(1, 50)]
    asks = [["10.02", "80"]] + [[str(round(10.02 + i * 0.01, 4)), "400"] for i in range(1, 50)]
    book = _book(bids, asks)

    assert book.obi(1) > 0.9, "the touch really is extreme"
    assert book.obi(50) < 0, "and the deep book really does disagree"

    alignment = book.alignment()
    assert alignment["top_book_score"] > 0.5
    assert alignment["deep_book_score"] < 0.2
    assert alignment["consistency"] < 0.8
    # The reading the score uses is a fraction of the touch's extremity.
    assert abs(alignment["book_alignment"]) < 0.3
    assert book.pressure_component() < 0.3
    assert "TOP_BOOK_BULLISH" in book.alignment_label()


def test_a_book_that_agrees_with_itself_scores_fully():
    bids = [[str(round(10.00 - i * 0.01, 4)), "500"] for i in range(50)]
    asks = [[str(round(10.02 + i * 0.01, 4)), "50"] for i in range(50)]
    book = _book(bids, asks)
    alignment = book.alignment()
    assert alignment["consistency"] > 0.95
    assert alignment["book_alignment"] > 0.7
    assert book.alignment_label() == "BOOK_BULLISH"


# ---- walls --------------------------------------------------------------

def test_a_wall_is_not_a_wall_the_moment_it_appears():
    """A large order that has stood for one second is not support. The
    first build could not tell a ten-second wall from a ten-minute one."""
    wall = Wall(side="bid", price=10.0, size=500.0, first_seen_ms=0, last_seen_ms=1_000)
    assert wall.classify(1_000) == "TRANSIENT_WALL"
    assert wall.weight(1_000) < 0.2
    wall.last_seen_ms = 40_000
    assert wall.classify(40_000) == "PERSISTENT_WALL"
    assert wall.weight(40_000) == 1.0


def test_a_wall_cut_and_rebuilt_is_a_defender():
    wall = Wall(side="bid", price=10.0, size=500.0, first_seen_ms=0, last_seen_ms=0)
    for size in (100.0, 500.0, 90.0, 480.0):
        wall.observe(size, wall.last_seen_ms + 1_000)
    assert wall.replenishments >= 2
    assert wall.classify(4_000) == "REPLENISHING_WALL"
    assert wall.weight(4_000) > 0.8


def test_a_wall_traded_through_is_absorbed_not_persistent():
    wall = Wall(side="bid", price=10.0, size=500.0, first_seen_ms=0, last_seen_ms=60_000)
    wall.executed = 450.0
    assert wall.classify(60_000) == "ABSORBED_WALL"
    assert wall.weight(60_000) < 0.3


def test_a_wall_that_vanishes_untouched_is_counted_as_a_spoof():
    book = _book([["10.00", "5000"]] + [[str(round(9.99 - i * 0.01, 4)), "10"] for i in range(20)],
                 [[str(round(10.02 + i * 0.01, 4)), "10"] for i in range(20)])
    assert book._walls, "the large bid registered as a wall"
    book.apply({"type": "delta", "ts": 2000,
                "data": {"u": 2, "b": [["10.00", "0"], ["9.98", "5"]], "a": []}})
    assert book.wall_summary(2000)["spoofs_60s"] >= 1


# ---- absorption ---------------------------------------------------------

def test_absorption_is_normalised_into_a_bounded_score():
    """The raw figure was `added / executed` - a number like 68815 on
    screen beside values living in -1..+1, feeding nothing."""
    from t3_engine.lead_engine.layers import absorption_scores

    book = _book([[str(round(10.00 - i * 0.01, 4)), "100"] for i in range(50)],
                 [[str(round(10.02 + i * 0.01, 4)), "100"] for i in range(50)])
    book.note_trade("Sell", 60.0, 10.00)
    book.apply({"type": "delta", "ts": 2000,
                "data": {"u": 2, "b": [["10.00", "160"]], "a": []}})
    scores = absorption_scores(book, Normalizer("TESTUSDT"))
    for key, value in scores.items():
        assert 0.0 <= value <= 1.0, f"{key} = {value} is outside 0..1"
    assert scores["bid_absorption_score"] > 0, "the bid was refilled after being hit"


# ---- layers and conflict ------------------------------------------------

def _layers(**scores):
    from t3_engine.lead_engine.layers import LAYERS

    return {name: LayerScore(name=name, score=scores.get(name) or 0.0,
                             confidence=0.0 if scores.get(name) is None else 1.0)
            for name in LAYERS}


def test_the_five_layers_are_scored_independently():
    result = pressure_score(_layers(flow=0.8, book=0.6, structure=0.4,
                                    derivatives=0.2, btc_lead=0.1))
    assert set(result.layers) == {"flow", "book", "structure", "derivatives", "btc_lead"}
    assert result.contributions["flow"] > result.contributions["btc_lead"], \
        "flow carries more weight than BTC lead"


def test_a_strong_long_is_refused_when_independent_sources_disagree():
    """The brief's rule. Structure bullish, flow and book bearish: do NOT
    emit a strong LONG."""
    result = pressure_score(_layers(structure=0.9, flow=-0.8, book=-0.7,
                                    derivatives=0.0, btc_lead=-0.1))
    assert result.conflict.level == CONFLICT_HIGH
    assert "structure" in result.conflict.opposing
    assert result.long_pressure < result.short_pressure
    assert result.confidence < 0.4


def test_unanimity_is_low_conflict_and_full_confidence():
    result = pressure_score(_layers(structure=0.7, flow=0.8, book=0.6,
                                    derivatives=0.5, btc_lead=0.4))
    assert result.conflict.level == CONFLICT_LOW
    assert result.conflict.opposing == []
    assert result.confidence > 0.9


def test_conflict_needs_at_least_two_layers_speaking():
    quiet = detect_conflict({"flow": LayerScore(name="flow", score=0.9, confidence=1.0)})
    assert quiet.level == CONFLICT_LOW and quiet.penalty == 1.0


def test_agreement_is_weighted_by_magnitude():
    assert agreement([0.8, 0.8, 0.8]) == pytest.approx(1.0)
    assert agreement([0.8, -0.8]) == pytest.approx(0.0)
    assert 0.0 < agreement([0.9, -0.1]) < 1.0


def test_split_direction_never_produces_a_complement():
    assert split_direction(0.0) == (0.0, 0.0), "a featureless reading is low on BOTH"
    assert split_direction(0.6) == (0.6, 0.0)
    assert split_direction(-0.6) == (0.0, 0.6)


# ---- calibration --------------------------------------------------------

def test_a_score_is_a_model_score_until_it_has_been_measured():
    """`break_probability` was a weighted feature mean with a percent sign
    next to it. Nothing had been measured."""
    calibrator = cal.Calibrator("TESTUSDT")
    assert calibrator.probability(65.0) is None
    label = cal.label_for(65.0, None)
    assert label["kind"] == "MODEL_SCORE"
    assert label["probability"] is None
    assert "not an empirical probability" in label["note"]


def test_a_bucket_becomes_a_probability_once_enough_cases_resolve():
    calibrator = cal.Calibrator("TESTUSDT", break_pct=0.002)
    rnd = random.Random(4)
    stamp = 1_000_000
    for _ in range(60):
        stamp += 200_000
        assert calibrator.observe("short", 65.0, 5.70, 5.705, stamp) is not None
        if rnd.random() < 0.7:
            calibrator.resolve(5.68, stamp + 8_000)
        calibrator.resolve(5.705, stamp + 31_000)
    rate = calibrator.probability(65.0, 15_000)
    assert rate is not None
    assert 0.55 < rate < 0.85, f"recovered {rate} from a 70% fixture"
    assert cal.label_for(65.0, rate)["kind"] == "PROBABILITY"


def test_calibration_never_resolves_from_a_price_that_is_not_later():
    """The no-lookahead boundary, enforced rather than assumed."""
    calibrator = cal.Calibrator("TESTUSDT")
    calibrator.observe("short", 70.0, 5.70, 5.705, 1_000)
    assert calibrator.resolve(1.0, 1_000) == 0, "a price at the same instant settles nothing"
    assert calibrator.resolve(1.0, 999) == 0, "nor does an earlier one"
    assert len(calibrator.pending) == 1


def test_one_setup_is_one_case_not_two_hundred():
    """A score sitting at 64 for a minute must not fill the bucket with
    copies of itself - the hit rate would become a measure of persistence."""
    calibrator = cal.Calibrator("TESTUSDT")
    first = calibrator.observe("short", 64.0, 5.70, 5.705, 1_000)
    again = calibrator.observe("short", 66.0, 5.70, 5.705, 1_500)
    assert first is not None and again is None


# ---- EMA ----------------------------------------------------------------

def test_ema_matches_a_hand_computed_control():
    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    alpha = 2.0 / (3 + 1)
    control = [None, None, 2.0]
    previous = 2.0
    for value in values[3:]:
        previous = alpha * value + (1 - alpha) * previous
        control.append(previous)
    assert [None if v is None else round(v, 9) for v in ema(values, 3)] == \
           [None if v is None else round(v, 9) for v in control]


def test_ema_emits_nothing_before_it_has_a_seed():
    assert ema([1, 2, 3], 5) == [None, None, None]
    line = ema(list(range(20)), 9)
    assert line[:8] == [None] * 8 and line[8] is not None


@pytest.mark.parametrize("period", [9, 18, 50, 200])
def test_every_required_ema_period_is_served(period):
    candles = [{"time": i * 60, "close": 100 + math.sin(i / 8.0) * 5} for i in range(400)]
    series = ema_series(candles)
    assert f"ema{period}" in series
    assert len(series[f"ema{period}"]) == 400 - period + 1


def test_interval_spellings_are_both_accepted():
    assert normalize_interval("60") == "1h"
    assert normalize_interval("1h") == "1h"
    assert normalize_interval("nonsense") is None


# ---- Fibonacci ----------------------------------------------------------

def test_fibonacci_is_not_inverted_in_either_direction():
    """Ratio 0 sits at the END of the move and 1.0 at its start, drawn
    either way round. Adding a direction conditional is what inverts it."""
    up = {level["ratio"]: level["price"] for level in levels(5.60, 6.00)}
    down = {level["ratio"]: level["price"] for level in levels(6.00, 5.60)}
    assert up[0.0] == pytest.approx(6.00) and up[1.0] == pytest.approx(5.60)
    assert down[0.0] == pytest.approx(5.60) and down[1.0] == pytest.approx(6.00)
    # 0.618 of a 0.40 move is 0.2472 back from the end, both ways.
    assert up[0.618] == pytest.approx(6.00 - 0.40 * 0.618)
    assert down[0.618] == pytest.approx(5.60 + 0.40 * 0.618)


def test_extensions_continue_past_the_start_of_the_move():
    up = {level["ratio"]: level["price"] for level in levels(5.60, 6.00)}
    assert up[1.618] < up[1.0] < up[0.0], "an extension of an up move projects lower"
    assert set(ALL_LEVELS) >= {0.236, 0.382, 0.5, 0.618, 0.705, 0.786,
                               1.272, 1.414, 1.618, 2.0, 2.618}


def test_drawings_are_kept_per_symbol_and_timeframe():
    """A level drawn on the 5m chart means nothing on the 1h one."""
    store = DrawingStore()
    store.add("INJUSDT", "5m", 1000, 5.60, 2000, 6.00)
    store.add("INJUSDT", "1h", 1000, 5.00, 2000, 7.00)
    assert len(store.list("INJUSDT", "5m")) == 1
    assert len(store.list("INJUSDT", "1h")) == 1
    assert store.list("INJUSDT", "5m")[0].start_price != \
           store.list("INJUSDT", "1h")[0].start_price
    store.reset("INJUSDT", "5m")
    assert store.list("INJUSDT", "5m") == []
    assert len(store.list("INJUSDT", "1h")) == 1, "resetting one chart left the other"


def test_a_drawing_reports_its_direction_without_changing_its_maths():
    store = DrawingStore()
    up = store.add("INJUSDT", "5m", 0, 5.0, 10, 6.0)
    down = store.add("INJUSDT", "5m", 0, 6.0, 10, 5.0)
    assert up.direction == "low_to_high" and down.direction == "high_to_low"
    assert up.as_dict()["levels"][0]["price"] == pytest.approx(6.0)
    assert down.as_dict()["levels"][0]["price"] == pytest.approx(5.0)
