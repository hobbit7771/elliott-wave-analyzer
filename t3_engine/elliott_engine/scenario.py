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

from t3_engine.common.models import Pivot, Scenario, Wave, next_id
from t3_engine.common.types import Direction, Timeframe, WaveLabel, WaveStatus
from t3_engine.elliott_engine.rules_impulse import validate_impulse
from t3_engine.fibonacci.calculator import (
    nearest_ratio_score,
    wave2_levels,
    wave4_levels,
)

MAX_SCENARIOS = 3
_IMPULSE_LABELS = [WaveLabel.W1, WaveLabel.W2, WaveLabel.W3, WaveLabel.W4, WaveLabel.W5]
_ABC_LABELS = [WaveLabel.A, WaveLabel.B, WaveLabel.C]


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


def build_candidate_waves(pivots: List[Pivot], direction: Direction, degree: Timeframe,
                          parent_wave_id: Optional[str] = None) -> Dict:
    """Build as many labelled waves as `pivots` allows (up to a full
    1-2-3-4-5-A-B-C), validating impulse hard rules as we go. `pivots[0]`
    is the anchor (start of Wave 1). Returns a dict with the wave list and
    invalidity info; stops adding waves the instant a hard rule breaks."""
    waves: List[Wave] = []
    broken_rule = None
    labels = _IMPULSE_LABELS + _ABC_LABELS

    for i in range(min(len(pivots) - 1, len(labels))):
        start, end = pivots[i], pivots[i + 1]
        label = labels[i]
        wave_direction = direction if label in (WaveLabel.W1, WaveLabel.W3, WaveLabel.W5, WaveLabel.B) else direction.opposite()
        if label == WaveLabel.A:
            wave_direction = direction.opposite()
        if label == WaveLabel.C:
            wave_direction = direction.opposite()
        wave = _make_wave(label, degree, start, end, wave_direction, parent_wave_id)
        waves.append(wave)

        if label == WaveLabel.W4:
            result = validate_impulse(waves[0], waves[1], waves[2], waves[3],
                                       waves[4] if len(waves) > 4 else None, direction)
            if not result.valid:
                broken_rule = result
                break
        elif label == WaveLabel.W5:
            result = validate_impulse(waves[0], waves[1], waves[2], waves[3], waves[4], direction)
            if not result.valid:
                broken_rule = result
                break

    if broken_rule is not None:
        waves[-1].status = WaveStatus.INVALIDATED

    return {"waves": waves, "broken_rule": broken_rule}


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
        self._normalize(survivors)
        self.scenarios = survivors
        return self.scenarios

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
    def _normalize(scenarios: List[Scenario]) -> None:
        total = sum(s.probability for s in scenarios)
        if total <= 0:
            return
        for s in scenarios:
            s.probability = round(100.0 * s.probability / total, 2)

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
