"""Probabilistic multi-scenario Elliott wave engine (spec section 7).

SCOPE NOTE: the spec describes an idealised engine that maintains an
unbounded tree of nested, simultaneously-evolving hypotheses at every
degree. A perfect implementation of that is a research problem (see the
limitation note in rules_correction.py - Elliott counting is inherently
ambiguous even to human experts). The engineering approximation used here,
which is deterministic, testable and genuinely causal:

  1. Candidate scenarios are generated from different recent pivots as the
     "start of Wave 1" anchor (this is exactly where real analysts
     disagree - "did the new trend start at swing A or swing B?").
  2. Each candidate is grown wave-by-wave against the hard Elliott rules
     (rules_impulse.py / rules_diagonal.py). The moment a hard rule is
     broken, that scenario's probability is forced to 0 and its status set
     to INVALIDATED - this happens unconditionally, before any soft score
     is even considered (section 7: hard rules always win).
  3. Surviving candidates are scored on a weighted blend of Fibonacci fit
     (computed here) plus externally-supplied price-action / volume /
     momentum / microstructure / derivatives scores (those come from other
     modules that watch the lower timeframes - this engine does not
     reimplement them).
  4. Only the top 3 surviving candidates by probability are kept.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from t3_engine.common.models import Candle, Pivot, Scenario, Wave, next_id
from t3_engine.common.types import Direction, StructureType, Timeframe, WaveLabel, WaveStatus
from t3_engine.elliott_engine.rules_correction import classify_correction
from t3_engine.elliott_engine.rules_diagonal import validate_diagonal
from t3_engine.elliott_engine.rules_impulse import validate_impulse
from t3_engine.fibonacci.calculator import (
    nearest_ratio_score,
    wave2_levels,
    wave4_levels,
)

MAX_SCENARIOS = 3
_IMPULSE_LABELS = [WaveLabel.W1, WaveLabel.W2, WaveLabel.W3, WaveLabel.W4, WaveLabel.W5]
_ABC_LABELS = [WaveLabel.A, WaveLabel.B, WaveLabel.C]
# Micro-degree subdivision of a single motive (W1/W3/W5) leg - same 5-wave
# shape and hard rules as the primary count, just one degree smaller and
# entirely inside that leg's own start/end. WaveLabel already reserves
# these values for exactly this (see its own docstring); build_subwaves()
# below is what actually produces them.
_MICRO_LABELS = [WaveLabel.I, WaveLabel.II, WaveLabel.III, WaveLabel.IV, WaveLabel.V]
_SAME_DIRECTION_AS_TREND = (WaveLabel.W1, WaveLabel.W3, WaveLabel.W5, WaveLabel.B,
                            WaveLabel.I, WaveLabel.III, WaveLabel.V)


def _make_wave(label: WaveLabel, degree: Timeframe, start: Pivot, end: Pivot,
               direction: Direction, parent_wave_id: Optional[str]) -> Wave:
    return Wave(
        wave_id=next_id("wave"),
        parent_wave_id=parent_wave_id,
        degree=degree,
        label=label,
        direction=direction,
        start_timestamp=start.timestamp,
        end_timestamp=end.timestamp,
        start_price=start.price,
        end_price=end.price,
        high=max(start.price, end.price),
        low=min(start.price, end.price),
        status=WaveStatus.CONFIRMED,
    )


def _rescue_as_diagonal(result, waves: List[Wave], wave5: Optional[Wave], direction: Direction) -> bool:
    """A wave-4/wave-1 overlap is only ever a diagonal, never a plain
    relabel, per rules_diagonal.py's own docstring: the overlap exception
    applies ONLY if the leg ALSO satisfies the diagonal-specific
    contracting/expanding shape rules (D3/D4) - a standard impulse that
    merely fails the overlap check is still invalidated below if this
    returns False. Tags all 5 legs DIAGONAL_ENDING on success (the far
    more common variant per that module's docstring - this engine has no
    parent-degree context to distinguish a leading vs. ending diagonal, a
    documented simplification, not a silent guess)."""
    if result.broken_rule != "WAVE4_OVERLAPS_WAVE1":
        return False
    diagonal_result = validate_diagonal(waves[0], waves[1], waves[2], waves[3], wave5, direction)
    if not diagonal_result.valid:
        return False
    for w in waves:
        w.structure_type = StructureType.DIAGONAL_ENDING
    return True


def build_candidate_waves(pivots: List[Pivot], direction: Direction, degree: Timeframe,
                          parent_wave_id: Optional[str] = None, labels: Optional[List[WaveLabel]] = None) -> Dict:
    """Build as many labelled waves as `pivots` allows (up to a full
    1-2-3-4-5-A-B-C, or - via `labels=_MICRO_LABELS` from build_subwaves() -
    a single motive leg's own i-ii-iii-iv-v subdivision), validating
    impulse hard rules as we go. `pivots[0]` is the anchor (start of the
    first wave). Returns a dict with the wave list and invalidity info;
    stops adding waves the instant a hard rule breaks.

    The wave-4/wave-5 hard-rule checks below trigger on POSITION (the 4th
    and 5th wave built), not on the digit labels themselves, precisely so
    this same function and the same rules work unchanged for the micro
    label scheme - a subwave count is held to the identical Elliott rules
    as the primary count, not a looser cosmetic approximation."""
    waves: List[Wave] = []
    broken_rule = None
    labels = labels if labels is not None else (_IMPULSE_LABELS + _ABC_LABELS)

    for i in range(min(len(pivots) - 1, len(labels))):
        start, end = pivots[i], pivots[i + 1]
        label = labels[i]
        wave_direction = direction if label in _SAME_DIRECTION_AS_TREND else direction.opposite()
        wave = _make_wave(label, degree, start, end, wave_direction, parent_wave_id)
        waves.append(wave)

        if len(waves) == 4:
            result = validate_impulse(waves[0], waves[1], waves[2], waves[3], None, direction)
            if not result.valid:
                if not _rescue_as_diagonal(result, waves, None, direction):
                    broken_rule = result
                    break
        elif len(waves) == 5:
            result = validate_impulse(waves[0], waves[1], waves[2], waves[3], waves[4], direction)
            if not result.valid:
                if not _rescue_as_diagonal(result, waves, waves[4], direction):
                    broken_rule = result
                    break
        elif label == WaveLabel.C:
            wave_a = next((w for w in waves if w.label == WaveLabel.A), None)
            wave_b = next((w for w in waves if w.label == WaveLabel.B), None)
            if wave_a is not None and wave_b is not None:
                # Only the 3-leg zigzag/flat classifier is wired in here -
                # triangles (A-B-C-D-E) and combinations (W-X-Y) need legs
                # this engine's fixed 8-label anchor scheme doesn't build
                # (see build_candidate_waves' `labels`), so those remain
                # UNKNOWN_CORRECTION for now rather than silently mislabelled.
                structure = classify_correction([wave_a, wave_b, wave])
                for w in (wave_a, wave_b, wave):
                    w.structure_type = structure

    if broken_rule is not None:
        waves[-1].status = WaveStatus.INVALIDATED

    return {"waves": waves, "broken_rule": broken_rule}


def build_subwaves(candles: List[Candle], parent_wave: Wave, deviation_pct: float) -> Dict:
    """Subdivide a single CONFIRMED motive wave (W1/W3/W5) into its own
    i-ii-iii-iv-v count - the spec follow-up that wave counting should
    "account for waves and subwaves", not just the top-level 1-5-A-B-C.

    Only ever called on a wave whose start/end are already fixed (it's
    been archived into ScenarioEngine.wave_history, i.e. the top-level
    count has already moved past it), so this is a one-shot, self-
    contained computation over exactly that wave's own candle range - a
    FRESH ZigZagPivotDetector at a smaller `deviation_pct` (finer than the
    parent degree's, since a subwave is by definition a smaller move) is
    run only over `candles`, and the result is graded by the identical
    hard Elliott rules as any primary count (see build_candidate_waves).
    A subwave count that fails those rules is exactly as invalid as a
    primary count that does - this is not a cosmetic decoration."""
    from t3_engine.market_structure.pivots import ZigZagPivotDetector

    detector = ZigZagPivotDetector(deviation_pct=deviation_pct)
    for i, candle in enumerate(candles):
        detector.update(i, candle)
    if len(detector.pivots) < 2:
        return {"waves": [], "broken_rule": None}

    # A confirmed pivot's OWN price is real, but pivots[0] here is whatever
    # the local detector first anchors on - not necessarily exactly
    # parent_wave.start_price. Prepend a synthetic anchor pivot at the
    # parent wave's real start so subwave 1 actually begins there, not at
    # the first LOCAL swing the smaller deviation happened to catch.
    anchor = Pivot(index=-1, timestamp=parent_wave.start_timestamp, price=parent_wave.start_price,
                   kind="LOW" if parent_wave.direction == Direction.UP else "HIGH",
                   confirmed_at_index=-1)
    pivots = [anchor] + list(detector.pivots)
    # Pivots must strictly alternate HIGH/LOW for build_candidate_waves to
    # produce sane legs - if the detector's first real pivot is the SAME
    # kind as the synthetic anchor (both "LOW", say), drop the anchor's
    # duplicate rather than feed two same-kind points in a row.
    if len(pivots) > 1 and pivots[1].kind == anchor.kind:
        pivots = pivots[1:]

    return build_candidate_waves(pivots, parent_wave.direction, parent_wave.degree,
                                  parent_wave_id=parent_wave.wave_id, labels=_MICRO_LABELS)


def score_fibonacci(waves: List[Wave]) -> float:
    """Average how well each retracement/extension wave in the sequence
    respects its ideal Fibonacci zone. Pure soft signal - never gates
    validity, only nudges probability (section 6: "Fibonacci is not a
    standalone signal")."""
    scores = []
    by_label = {w.label: w for w in waves}

    if WaveLabel.W1 in by_label and WaveLabel.W2 in by_label:
        w1, w2 = by_label[WaveLabel.W1], by_label[WaveLabel.W2]
        levels = wave2_levels(w1.start_price, w1.end_price)
        scores.append(nearest_ratio_score(w2.end_price, levels, w1.length))

    if WaveLabel.W3 in by_label and WaveLabel.W4 in by_label:
        w3, w4 = by_label[WaveLabel.W3], by_label[WaveLabel.W4]
        levels = wave4_levels(w3.start_price, w3.end_price)
        scores.append(nearest_ratio_score(w4.end_price, levels, w3.length))

    return sum(scores) / len(scores) if scores else 0.5  # neutral prior if nothing to compare yet


@dataclass
class ScenarioEngine:
    """Tracks the probabilistic top-N scenarios for one (symbol, degree)."""

    degree: Timeframe
    max_scenarios: int = MAX_SCENARIOS
    weight_elliott: float = 0.30
    weight_fibonacci: float = 0.15
    weight_price_action: float = 0.20
    weight_volume: float = 0.10
    weight_momentum: float = 0.10
    weight_derivatives: float = 0.05
    weight_microstructure: float = 0.10

    scenarios: List[Scenario] = field(default_factory=list)
    # `rebuild()` REPLACES `scenarios` from scratch every time (see its own
    # docstring) - correct for "what's the current best count", but it means
    # every earlier wave that was ever labelled gets silently thrown away
    # the moment a new pivot confirms, and a chart built only from
    # `scenarios` shows numbered waves at the tail of the history with
    # nothing behind them. This is a running, append-only log of every wave
    # any TOP-ranked scenario ever confirmed, keyed by (label,
    # start_timestamp) so re-adding the same wave on a later rebuild (the
    # common case: the anchor is unchanged and the count just grew one more
    # wave) is a no-op rather than a duplicate. The CURRENTLY-forming last
    # wave of the top scenario is deliberately excluded each time - its end
    # price/time is still provisional until the next pivot confirms it, so
    # only once a rebuild adds a wave AFTER it does it become final and get
    # recorded here.
    wave_history: Dict[tuple, Wave] = field(default_factory=dict)
    _external_scores: Dict[str, Dict[str, float]] = field(default_factory=dict)

    def set_external_scores(self, scenario_id: str, *, price_action: float = 0.0, volume: float = 0.0,
                             momentum: float = 0.0, derivatives: float = 0.0, microstructure: float = 0.0) -> None:
        """Signal/orderflow/derivatives modules push their independently-
        computed scores in here; this engine only owns Elliott validity +
        Fibonacci, which are the two highest-weighted per section 23."""
        self._external_scores[scenario_id] = {
            "price_action": price_action, "volume": volume, "momentum": momentum,
            "derivatives": derivatives, "microstructure": microstructure,
        }

    def rebuild(self, pivot_history: List[Pivot], direction: Direction) -> List[Scenario]:
        """Regenerate scenarios from the pivot history. Called every time a
        new pivot is confirmed. Anchors: the most recent 1, 2 and 3 pivots
        back from the newest same-kind pivot as Wave-1-start hypotheses -
        this is the concrete stand-in for "analysts disagree where the
        move started" described in the module docstring."""
        anchor_kind = "LOW" if direction == Direction.UP else "HIGH"
        same_kind_indices = [i for i, p in enumerate(pivot_history) if p.kind == anchor_kind]

        new_scenarios: List[Scenario] = []
        for anchor_idx in same_kind_indices[-self.max_scenarios:]:
            sub_pivots = pivot_history[anchor_idx:]
            if len(sub_pivots) < 2:
                continue
            built = build_candidate_waves(sub_pivots, direction, self.degree)
            waves = built["waves"]
            if not waves:
                continue

            scenario_id = next_id("scenario")
            invalidated = built["broken_rule"] is not None
            fib_score = score_fibonacci(waves)
            ext = self._external_scores.get(scenario_id, {})

            elliott_validity = 0.0 if invalidated else min(1.0, 0.5 + 0.1 * len(waves))
            scenario = Scenario(
                scenario_id=scenario_id,
                degree=self.degree,
                waves=waves,
                elliott_validity=elliott_validity,
                fib_score=fib_score,
                price_action_score=ext.get("price_action", 0.5),
                volume_score=ext.get("volume", 0.5),
                momentum_score=ext.get("momentum", 0.5),
                microstructure_score=ext.get("microstructure", 0.5),
                derivatives_score=ext.get("derivatives", 0.5),
                status=WaveStatus.INVALIDATED if invalidated else WaveStatus.DEVELOPING,
                invalidation=self._invalidation_level(waves, direction),
                next_expected_label=_next_label(waves[-1].label) if waves else None,
            )
            scenario.probability = 0.0 if invalidated else self._weighted_score(scenario)
            new_scenarios.append(scenario)

        new_scenarios.sort(key=lambda s: s.probability, reverse=True)
        survivors = [s for s in new_scenarios if s.probability > 0][: self.max_scenarios]
        self._scale_to_percent(survivors)
        self.scenarios = survivors
        self._record_wave_history(survivors)
        return self.scenarios

    def _record_wave_history(self, survivors: List[Scenario]) -> None:
        if not survivors:
            return
        top = survivors[0]
        for wave in top.waves[:-1]:
            self.wave_history[(wave.label, wave.start_timestamp)] = wave

    def _weighted_score(self, s: Scenario) -> float:
        return (
            self.weight_elliott * s.elliott_validity
            + self.weight_fibonacci * s.fib_score
            + self.weight_price_action * s.price_action_score
            + self.weight_volume * s.volume_score
            + self.weight_momentum * s.momentum_score
            + self.weight_derivatives * s.derivatives_score
            + self.weight_microstructure * s.microstructure_score
        )

    @staticmethod
    def _scale_to_percent(scenarios: List[Scenario]) -> None:
        """Each scenario's OWN absolute score, scaled to 0-100 - deliberately
        NOT redistributed/renormalized across survivors (the previous
        behavior: dividing by the sum of survivors' scores meant a single
        weak scenario that outlived the others got rescaled to exactly
        100%, so `entry_confidence_threshold` silently meant "highest
        share among whatever's left" instead of "at least this good on an
        absolute scale"). `_weighted_score` already returns a value in
        [0, 1] since its component scores are each bounded in [0, 1] and
        its weights sum to 1.0, so a plain *100 here is a legitimate
        absolute percentage."""
        for s in scenarios:
            s.probability = round(100.0 * s.probability, 2)

    @staticmethod
    def _invalidation_level(waves: List[Wave], direction: Direction) -> Optional[float]:
        if not waves:
            return None
        last = waves[-1]
        # Structural invalidation for the currently-open wave: for a
        # continuing impulse this is wave 1's start (wave 2 rule); the
        # exact level depends on which wave is open, kept simple/causal here.
        w1 = next((w for w in waves if w.label == WaveLabel.W1), None)
        return w1.start_price if w1 else last.start_price


def _next_label(current: WaveLabel) -> Optional[WaveLabel]:
    order = _IMPULSE_LABELS + _ABC_LABELS
    if current not in order:
        return None
    idx = order.index(current)
    return order[idx + 1] if idx + 1 < len(order) else None
