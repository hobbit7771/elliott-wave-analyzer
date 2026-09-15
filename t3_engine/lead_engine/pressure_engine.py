"""Five layer scores into LONG_PRESSURE and SHORT_PRESSURE.

Rewritten. The first version summed nine flat components in one pass,
which made a reading impossible to attribute and made disagreement
invisible. Now `layers.py` produces five independent readings - flow,
book, structure, derivatives, BTC lead - and this module does three
things with them and nothing else:

  1. weights them (config.LayerWeights, all five overridable),
  2. measures how much they disagree (layers.detect_conflict),
  3. reports a CONFIDENCE that the disagreement reduces.

Two design decisions carried over from the first version because they
were right, and one that is new.

LONG and SHORT are computed SEPARATELY, not as a number and its
complement. A featureless market must read low on both; a contested one
must read high on both, because that is a real and distinct state.

A layer that cannot answer is DROPPED and its weight redistributed, never
counted as a neutral zero - which would dilute a genuine reading toward
the middle and make a half-connected engine look calm instead of
uninformed.

New: the conflict penalty multiplies CONFIDENCE, not the score. A
contested market still reports what each layer sees. It just stops
claiming to be sure, which is a different and more useful statement than
quietly halving the number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from t3_engine.lead_engine.config import LayerWeights
from t3_engine.lead_engine.layers import (
    LAYERS,
    Conflict,
    LayerScore,
    detect_conflict,
)
from t3_engine.lead_engine.normalize import clamp

# What the engine calls its own output. Deliberately not "probability":
# nothing here has been calibrated against what actually happened, and a
# number labelled as a probability will be read as one. See
# calibration.py for what it takes to earn the other word.
MODEL_SCORE = "MODEL_SCORE"


@dataclass
class PressureResult:
    long_pressure: float = 0.0
    short_pressure: float = 0.0
    net: float = 0.0
    confidence: float = 0.0
    conflict: Optional[Conflict] = None
    layers: Dict[str, LayerScore] = field(default_factory=dict)
    contributions: Dict[str, float] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)
    label: str = MODEL_SCORE

    def as_dict(self) -> Dict[str, Any]:
        return {
            "long_pressure": round(self.long_pressure, 2),
            "short_pressure": round(self.short_pressure, 2),
            "net": round(self.net, 2),
            "confidence": round(self.confidence, 3),
            # Kept for anything still reading the old field. It is the
            # smaller of the two sides, which is what it always was.
            "conflict": round(min(self.long_pressure, self.short_pressure), 2),
            "conflict_detail": self.conflict.as_dict() if self.conflict else None,
            "layers": {name: layer.as_dict() for name, layer in self.layers.items()},
            "contributions": {k: round(v, 4) for k, v in self.contributions.items()},
            "missing": list(self.missing),
            "label": self.label,
            "explanation": describe(self),
        }


def score(layers: Dict[str, LayerScore],
          weights: Optional[LayerWeights] = None) -> PressureResult:
    """Combine the five layers. Never raises on a partial set."""
    weights = weights or LayerWeights()
    table = weights.as_dict()

    present = {name: layer for name, layer in layers.items()
               if layer is not None and layer.confidence > 0}
    missing = [name for name in LAYERS if name not in present]

    result = PressureResult(layers={name: layer for name, layer in layers.items()
                                    if layer is not None},
                            missing=missing)
    available_weight = sum(table.get(name, 0.0) for name in present)
    if available_weight <= 0:
        result.conflict = detect_conflict(present)
        return result

    long_total = short_total = 0.0
    for name, layer in present.items():
        share = table[name] / available_weight
        result.contributions[name] = round(share * layer.score, 6)
        long_total += share * layer.long
        short_total += share * layer.short

    result.long_pressure = round(100.0 * long_total, 4)
    result.short_pressure = round(100.0 * short_total, 4)
    result.net = round(result.long_pressure - result.short_pressure, 4)

    result.conflict = detect_conflict(present)
    # Confidence: how much of the weight answered, how sure those layers
    # were, and how much they agreed. All three multiply, because any one
    # of them being low is enough to make the number untrustworthy.
    coverage = available_weight / max(1e-9, sum(table.values()))
    layer_confidence = (sum(table[name] * layer.confidence for name, layer in present.items())
                        / available_weight)
    result.confidence = clamp(coverage * layer_confidence * result.conflict.penalty,
                              0.0, 1.0)
    return result


def describe(result: PressureResult, top: int = 3) -> str:
    """One line naming what is actually driving the number.

    A score with no attribution cannot be judged. "82 long" backed by the
    book and the flow is a different claim from "82 long" backed by a
    heuristic carrying a tenth of the weight."""
    if not result.contributions:
        return "No layer has enough data to score yet."
    ordered = sorted(result.contributions.items(), key=lambda kv: abs(kv[1]), reverse=True)
    parts = [f"{name} {value:+.2f}" for name, value in ordered[:top] if abs(value) > 0.005]
    if not parts:
        return "Every layer is neutral."
    side = "long" if result.net >= 0 else "short"
    line = f"{side} {abs(result.net):.0f} from " + ", ".join(parts)
    if result.conflict and result.conflict.opposing:
        line += f" — but {result.conflict.note}"
    return line
