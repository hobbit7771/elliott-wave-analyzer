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


STRUCTURE_IMPULSE = "IMPULSE"
STRUCTURE_DIAGONAL_CONTRACTING = "DIAGONAL_CONTRACTING"
STRUCTURE_DIAGONAL_EXPANDING = "DIAGONAL_EXPANDING"


def build_candidate_waves(pivots: List[Pivot], direction: Direction, degree: Timeframe,
                          parent_wave_id: Optional[str] = None, labels: Optional[List[WaveLabel]] = None,
                          structure: str = STRUCTURE_IMPULSE) -> Dict:
    """Build as many labelled waves as `pivots` allows (up to a full
    1-2-3-4-5-A-B-C, or - via `labels=_MICRO_LABELS` from build_subwaves() -
    a single motive leg's own i-ii-iii-iv-v subdivision), validating the
    hard rules as we go. `pivots[0]` is the anchor (start of the first
    wave). Returns a dict with the wave list and invalidity info; stops
    adding waves the instant a hard rule breaks.

    `structure` decides WHICH hard-rule set the motive legs are held to,
    and it is chosen UP FRONT by the caller, never switched mid-count:

      IMPULSE              -> rules_impulse.validate_impulse. A wave-4
                              overlap here is fatal, full stop.
      DIAGONAL_CONTRACTING -> rules_diagonal.validate_diagonal. The
      DIAGONAL_EXPANDING      overlap exception applies, but ONLY together
                              with the diagonal's own shape rules (D3/D4).

    An earlier version "rescued" a broken impulse by silently relabelling
    it a diagonal the moment it failed the overlap rule. That inverted the
    burden of proof (rules_diagonal.py's own docstring: a diagonal must be
    PROVEN, not assumed) and let a count that had already failed its rules
    stay on the chart under a different name. Now a diagonal is a separate,
    independently-generated hypothesis that has to earn its place against
    the impulse reading on its own merits - a failed impulse just dies.

    The motive hard-rule checks trigger on POSITION (the 4th and 5th wave
    built), not on the digit labels themselves, precisely so this same
    function and the same rules work unchanged for the micro label scheme -
    a subwave count is held to the identical Elliott rules as the primary
    count, not a looser cosmetic approximation."""
    waves: List[Wave] = []
    broken_rule = None
    motive_validated = False
    labels = labels if labels is not None else (_IMPULSE_LABELS + _ABC_LABELS)
    is_diagonal = structure != STRUCTURE_IMPULSE
    variant = "EXPANDING" if structure == STRUCTURE_DIAGONAL_EXPANDING else "CONTRACTING"

    for i in range(min(len(pivots) - 1, len(labels))):
        label = labels[i]
        # A corrective A-B-C only exists as the correction OF something.
        # Without a motive structure that has already PASSED its own hard
        # rules underneath it, there is nothing to correct, so these labels
        # are never appended on spec - previously this held only by
        # accident (the loop happened to break earlier on a broken motive),
        # which is not the same as being enforced.
        if label in _ABC_LABELS and not motive_validated:
            break

        start, end = pivots[i], pivots[i + 1]
        wave_direction = direction if label in _SAME_DIRECTION_AS_TREND else direction.opposite()
        wave = _make_wave(label, degree, start, end, wave_direction, parent_wave_id)
        waves.append(wave)

        if len(waves) == 4:
            result = (validate_diagonal(waves[0], waves[1], waves[2], waves[3], None, direction, variant)
                      if is_diagonal else
                      validate_impulse(waves[0], waves[1], waves[2], waves[3], None, direction))
            if not result.valid:
                broken_rule = result
                break
        elif len(waves) == 5:
            result = (validate_diagonal(waves[0], waves[1], waves[2], waves[3], waves[4], direction, variant)
                      if is_diagonal else
                      validate_impulse(waves[0], waves[1], waves[2], waves[3], waves[4], direction))
            if not result.valid:
                broken_rule = result
                break
            motive_validated = True
        elif label == WaveLabel.C:
            wave_a = next((w for w in waves if w.label == WaveLabel.A), None)
            wave_b = next((w for w in waves if w.label == WaveLabel.B), None)
            if wave_a is not None and wave_b is not None:
                # Only the 3-leg zigzag/flat classifier is wired in here -
                # triangles (A-B-C-D-E) and combinations (W-X-Y) need legs
                # this engine's fixed 8-label anchor scheme doesn't build
                # (see build_candidate_waves' `labels`), so those remain
                # UNKNOWN_CORRECTION for now rather than silently mislabelled.
                structure_type = classify_correction([wave_a, wave_b, wave])
                for w in (wave_a, wave_b, wave):
                    w.structure_type = structure_type

    if broken_rule is not None:
        waves[-1].status = WaveStatus.INVALIDATED
    elif is_diagonal:
        # This engine has no parent-degree context to tell a leading from
        # an ending diagonal, so it tags the far more common variant rather
        # than guessing - a documented simplification, not a silent claim.
        for w in waves[:5]:
            w.structure_type = StructureType.DIAGONAL_ENDING

    return {"waves": waves, "broken_rule": broken_rule, "structure": structure,
            "motive_validated": motive_validated}


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
    # ONE globally consistent chain of confirmed structures - never an
    # archive of every local guess.
    #
    # `rebuild()` REPLACES `scenarios` from scratch on every new pivot, so
    # something has to remember what came before or the chart shows numbers
    # only at the tail. The previous attempt at that was an append-only log
    # keyed by (label, start_timestamp), which was wrong in a way that only
    # shows up on a long chart: when the engine re-anchors, the NEW count
    # disagrees with the old one about the same stretch of time, and both
    # readings stayed on the chart forever - two contradictory "wave 3"s
    # over the same candles, accumulating with every re-anchor.
    #
    # The invariant now: every point in time is covered by AT MOST ONE
    # wave. `_extend_confirmed_chain` drops any chain entry the current
    # reading overlaps before appending that reading's completed waves, so
    # re-anchoring truncates history back to the divergence point instead
    # of layering another guess on top of it. The still-forming last wave
    # of the top scenario is deliberately excluded - its end is provisional
    # until a later pivot confirms it - so the chain only ever holds waves
    # whose geometry is already fixed.
    confirmed_chain: List[Wave] = field(default_factory=list)
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
            # Impulse and diagonal are generated as INDEPENDENT hypotheses
            # over the same pivots, each validated by its own rule set, and
            # then left to compete on probability. A diagonal is never a
            # fallback applied to a count that already failed the impulse
            # rules (see build_candidate_waves' docstring).
            for structure in (STRUCTURE_IMPULSE, STRUCTURE_DIAGONAL_CONTRACTING, STRUCTURE_DIAGONAL_EXPANDING):
                built = build_candidate_waves(sub_pivots, direction, self.degree, structure=structure)
                scenario = self._score_candidate(built, direction)
                if scenario is not None:
                    new_scenarios.append(scenario)

        new_scenarios.sort(key=lambda s: s.probability, reverse=True)
        survivors = [s for s in new_scenarios if s.probability > 0]
        survivors = self._drop_duplicate_geometry(survivors)[: self.max_scenarios]
        self._scale_to_percent(survivors)
        self.scenarios = survivors
        self._extend_confirmed_chain(survivors)
        return self.scenarios

    def _score_candidate(self, built: Dict, direction: Direction) -> Optional[Scenario]:
        waves = built["waves"]
        if not waves:
            return None

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
            next_expected_label=_next_label(waves[-1].label),
        )
        scenario.probability = 0.0 if invalidated else self._weighted_score(scenario)
        return scenario

    @staticmethod
    def _drop_duplicate_geometry(scenarios: List[Scenario]) -> List[Scenario]:
        """Until a count reaches its 4th wave, the impulse and diagonal
        readings of the same pivots are the same lines on the chart (no
        rule has distinguished them yet). Keeping both would just render
        one overlay twice and crowd out a genuinely different anchor, so
        only the highest-scoring reading of any given geometry survives -
        scenarios are already sorted by probability here."""
        seen = set()
        unique = []
        for s in scenarios:
            geometry = tuple((w.label, w.start_timestamp, w.end_timestamp) for w in s.waves)
            if geometry in seen:
                continue
            seen.add(geometry)
            unique.append(s)
        return unique

    def _extend_confirmed_chain(self, survivors: List[Scenario]) -> None:
        """Fold the top scenario's already-fixed waves into the single
        consistent chain, dropping whatever the new reading supersedes.

        Time, not label identity, is what makes this consistent: any chain
        entry that runs into the stretch the current count now covers is a
        superseded interpretation of those same candles, so it's removed
        rather than left to contradict the new one. Entries that end at or
        before the new count's anchor describe earlier structures the
        current count says nothing about, and are kept untouched."""
        if not survivors:
            return
        completed = survivors[0].waves[:-1]  # last wave's end is still provisional
        if not completed:
            return
        segment_start = completed[0].start_timestamp
        self.confirmed_chain = [w for w in self.confirmed_chain if w.end_timestamp <= segment_start]
        self.confirmed_chain.extend(completed)

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
