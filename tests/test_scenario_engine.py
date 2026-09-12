from t3_engine.backtest.synthetic_data import generate_synthetic_impulse_cycle
from t3_engine.common.models import Pivot, Wave
from t3_engine.common.types import Direction, StructureType, Timeframe, WaveLabel, WaveStatus
from t3_engine.elliott_engine.scenario import ScenarioEngine, build_candidate_waves, build_subwaves


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


def test_scenario_engine_accumulates_wave_history_across_rebuilds():
    """rebuild() REPLACES `scenarios` from scratch every call (see its
    docstring) - fine for "what's the current best count", but on its own
    it means the chart only ever shows numbered waves at the tail of the
    history, with every earlier wave silently discarded the moment a new
    pivot confirms. `wave_history` is the fix: each wave the top scenario
    ever confirms (i.e. is no longer its still-forming last wave) should
    stay recorded permanently, so the chart can number waves across the
    WHOLE history, not just the latest handful."""
    engine = ScenarioEngine(degree=Timeframe.M5)
    pivots = [
        pivot(0, 100, "LOW"),
        pivot(1, 150, "HIGH"),
        pivot(2, 120, "LOW"),
        pivot(3, 250, "HIGH"),
        pivot(4, 200, "LOW"),
        pivot(5, 280, "HIGH"),
    ]

    engine.rebuild(pivots[:4], Direction.UP)  # builds W1, W2, W3(forming)
    assert set(engine.wave_history.keys()) == {("1", pivots[0].timestamp), ("2", pivots[1].timestamp)}

    engine.rebuild(pivots[:5], Direction.UP)  # builds W1..W4(forming) - W3 now confirmed
    assert set(engine.wave_history.keys()) == {
        ("1", pivots[0].timestamp), ("2", pivots[1].timestamp), ("3", pivots[2].timestamp),
    }

    engine.rebuild(pivots, Direction.UP)  # full W1..W5(forming) - W4 now confirmed too
    assert set(engine.wave_history.keys()) == {
        ("1", pivots[0].timestamp), ("2", pivots[1].timestamp),
        ("3", pivots[2].timestamp), ("4", pivots[3].timestamp),
    }
    # Never discarded once recorded, even though `scenarios` itself just
    # got replaced again on this very call.
    assert engine.wave_history[("1", pivots[0].timestamp)].start_price == 100


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


def test_wave_to_dict_exposes_structure_type():
    """The classification wiring is only useful if it actually reaches the
    API response - not just an internal field nobody serializes."""
    from t3_engine.dashboard.serialization import wave_to_dict

    pivots = [
        pivot(0, 100, "LOW"), pivot(1, 140, "HIGH"), pivot(2, 110, "LOW"),
        pivot(3, 130, "HIGH"), pivot(4, 115, "LOW"), pivot(5, 125, "HIGH"),
    ]
    result = build_candidate_waves(pivots, Direction.UP, Timeframe.M5)
    d = wave_to_dict(result["waves"][0])
    assert d["structure_type"] == "DIAGONAL_ENDING"


def test_wave4_overlap_rescued_as_diagonal_when_shape_actually_contracts():
    """A wave4/wave1 overlap must NOT be automatically relabelled a
    diagonal - only rescued when the leg ALSO satisfies the diagonal
    shape rules (contracting: |3|<|1|, |5|<|3|). This pivot set overlaps
    AND genuinely contracts, so it should survive as a tagged diagonal
    instead of being invalidated."""
    pivots = [
        pivot(0, 100, "LOW"),
        pivot(1, 140, "HIGH"),   # wave1, length 40
        pivot(2, 110, "LOW"),    # wave2 (doesn't retrace past 100)
        pivot(3, 130, "HIGH"),   # wave3, length 20 (< wave1's 40)
        pivot(4, 115, "LOW"),    # wave4, overlaps wave1 (115 <= wave1.high 140)
        pivot(5, 125, "HIGH"),   # wave5, length 10 (< wave3's 20)
    ]
    result = build_candidate_waves(pivots, Direction.UP, Timeframe.M5)
    assert result["broken_rule"] is None
    assert len(result["waves"]) == 5
    assert all(w.structure_type == StructureType.DIAGONAL_ENDING for w in result["waves"])


def test_wave4_overlap_not_rescued_when_shape_does_not_contract():
    """The existing invalidation test already covers this pivot set (wave3
    is much LONGER than wave1, the opposite of contracting) - this test
    makes the reasoning explicit: the diagonal rescue must not fire just
    because an overlap occurred."""
    pivots = [
        pivot(0, 100, "LOW"),
        pivot(1, 150, "HIGH"),
        pivot(2, 120, "LOW"),
        pivot(3, 250, "HIGH"),   # wave3, length 130 - NOT shorter than wave1 (50)
        pivot(4, 130, "LOW"),    # overlaps wave1
    ]
    result = build_candidate_waves(pivots, Direction.UP, Timeframe.M5)
    assert result["broken_rule"] is not None
    assert result["waves"][-1].structure_type is None


def test_abc_correction_gets_classified_after_a_full_impulse():
    """Real gap found in an independent audit: rules_correction.py existed
    but was never actually called from build_candidate_waves - pivots were
    relabelled straight into A/B/C text with no shape classification at
    all. This is a full, valid 1-2-3-4-5 impulse followed by a zigzag
    (B retraces 55% of A - above the ~50% combination-connector threshold,
    below the 78.6% zigzag ceiling) A-B-C correction."""
    pivots = [
        pivot(0, 100, "LOW"),
        pivot(1, 150, "HIGH"),   # wave1
        pivot(2, 120, "LOW"),    # wave2
        pivot(3, 250, "HIGH"),   # wave3
        pivot(4, 160, "LOW"),    # wave4 (no overlap: 160 > wave1.high 150)
        pivot(5, 280, "HIGH"),   # wave5
        pivot(6, 200, "LOW"),    # wave A, length 80
        pivot(7, 244, "HIGH"),   # wave B, retraces 44/80 = 55% of A
        pivot(8, 140, "LOW"),    # wave C
    ]
    result = build_candidate_waves(pivots, Direction.UP, Timeframe.M5)
    assert result["broken_rule"] is None
    assert len(result["waves"]) == 8
    abc = result["waves"][5:8]
    assert [w.label.value for w in abc] == ["A", "B", "C"]
    assert all(w.structure_type == StructureType.ZIGZAG for w in abc)


def test_lone_survivor_keeps_its_own_absolute_score_not_rescaled_to_100():
    """Real bug found in an independent audit: probability used to be
    normalized across survivors (divide by their sum), so a single weak
    scenario that outlived the others was rescaled to exactly 100% -
    silently redefining entry_confidence_threshold as "highest share among
    whatever's left" rather than "at least this good on an absolute
    0-100 scale". With only one candidate anchor available, its score
    must now be its own weighted score, scaled to a 0-100 percent, and
    nothing else."""
    engine = ScenarioEngine(degree=Timeframe.M5)
    pivots = [pivot(0, 90, "LOW"), pivot(1, 95, "HIGH")]  # only one possible anchor/candidate
    scenarios = engine.rebuild(pivots, Direction.UP)
    assert len(scenarios) == 1
    assert scenarios[0].probability < 100.0
    assert scenarios[0].probability == round(100.0 * engine._weighted_score(scenarios[0]), 2)


# ---- subwaves (spec follow-up: "waves and subwaves should be accounted
# for" - a motive wave (1/3/5) subdivides into its own i-ii-iii-iv-v count,
# held to the identical hard Elliott rules as the primary count) ----

def _parent_wave(candles, label=WaveLabel.W3):
    return Wave(wave_id="parent-1", parent_wave_id=None, degree=Timeframe.M5, label=label,
                direction=Direction.UP, start_timestamp=candles[0].open_time,
                end_timestamp=candles[-1].close_time, start_price=candles[0].open,
                end_price=candles[-1].close, high=max(c.high for c in candles),
                low=min(c.low for c in candles))


def test_build_subwaves_produces_micro_labels_tagged_with_the_parent_wave_id():
    candles = generate_synthetic_impulse_cycle(seed=7)
    parent = _parent_wave(candles)
    result = build_subwaves(candles, parent, deviation_pct=1.0)
    assert isinstance(result["waves"], list)
    micro_labels = (WaveLabel.I, WaveLabel.II, WaveLabel.III, WaveLabel.IV, WaveLabel.V)
    for w in result["waves"]:
        assert w.label in micro_labels
        assert w.parent_wave_id == "parent-1"
        assert w.degree == parent.degree


def test_build_subwaves_empty_with_too_few_candles():
    candles = generate_synthetic_impulse_cycle(seed=7)[:2]
    parent = _parent_wave(candles)
    result = build_subwaves(candles, parent, deviation_pct=1.0)
    assert result == {"waves": [], "broken_rule": None}


def test_build_subwaves_held_to_the_same_hard_rules_as_a_primary_count():
    """A subwave count that overlaps wave-1/wave-4-style must be flagged
    INVALIDATED exactly like a primary count would be - not a looser,
    cosmetic-only approximation."""
    from t3_engine.common.types import WaveStatus as WS
    pivots_as_wave1 = [
        pivot(0, 100, "LOW"), pivot(1, 150, "HIGH"), pivot(2, 120, "LOW"),
        pivot(3, 250, "HIGH"), pivot(4, 130, "LOW"),  # overlaps micro-I's high (150)
    ]
    # Reuse build_candidate_waves directly with the micro label scheme to
    # confirm the SAME overlap rule fires - build_subwaves is a thin
    # candle->pivot wrapper around exactly this.
    from t3_engine.elliott_engine.scenario import _MICRO_LABELS
    result = build_candidate_waves(pivots_as_wave1, Direction.UP, Timeframe.M5, labels=_MICRO_LABELS)
    assert result["broken_rule"] is not None
    assert result["waves"][-1].status == WS.INVALIDATED
