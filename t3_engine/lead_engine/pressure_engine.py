"""Nine component scores into two numbers: LONG_PRESSURE and SHORT_PRESSURE.

Each contributing module returns one number on -1..+1, positive meaning
"this favours the upside". The weights come from config.PressureWeights -
the specification's opening values, kept as data so they can be argued
with and changed without touching this arithmetic.

The one design decision worth stating: LONG and SHORT are computed
SEPARATELY rather than as one number and its complement. Long = 100 minus
short would mean a completely balanced, featureless market reads as
"50 long pressure", which sounds like a position. Here, a market with
nothing happening scores low on both, a market pulling hard in one
direction scores high on one and low on the other, and a market being
fought over scores high on BOTH - which is a real and distinct state, and
one worth seeing before taking a trade in either direction.

`conflict` reports exactly that: how much of the total weight is pointing
each way at once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from t3_engine.lead_engine.config import PressureWeights
from t3_engine.lead_engine.rolling import clamp

# The nine components, in the order the specification lists them. Named
# here so a missing one is caught rather than silently scoring zero.
COMPONENTS: Tuple[str, ...] = (
    "order_book_imbalance",
    "microprice",
    "cvd",
    "trade_velocity",
    "liquidity_shift",
    "liquidations",
    "btc_lead_lag",
    "smc",
    "elliott_context",
)


@dataclass
class PressureResult:
    long_pressure: float = 0.0
    short_pressure: float = 0.0
    net: float = 0.0
    conflict: float = 0.0
    components: Dict[str, float] = field(default_factory=dict)
    contributions: Dict[str, float] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return {
            "long_pressure": round(self.long_pressure, 2),
            "short_pressure": round(self.short_pressure, 2),
            "net": round(self.net, 2),
            "conflict": round(self.conflict, 2),
            "components": {k: round(v, 4) for k, v in self.components.items()},
            "contributions": {k: round(v, 4) for k, v in self.contributions.items()},
            "missing": list(self.missing),
        }


def score(components: Dict[str, Optional[float]],
          weights: Optional[PressureWeights] = None) -> PressureResult:
    """Weighted pressure from whatever components are available.

    A component that is None - not yet computable, its stream not yet
    flowing - is DROPPED and its weight redistributed over the rest,
    rather than counted as a neutral zero. Counting it as zero would dilute
    a genuine reading toward the middle and make a half-connected engine
    look calm instead of uninformed; `missing` says which ones those
    were so the UI can show it."""
    weights = weights or PressureWeights()
    table = weights.as_dict()

    present: Dict[str, float] = {}
    missing: List[str] = []
    for name in COMPONENTS:
        value = components.get(name)
        if value is None:
            missing.append(name)
            continue
        present[name] = clamp(float(value))

    available_weight = sum(table[name] for name in present)
    result = PressureResult(components=dict(present), missing=missing)
    if available_weight <= 0:
        return result

    long_total = short_total = 0.0
    for name, value in present.items():
        weight = table[name] / available_weight
        result.contributions[name] = round(weight * value, 6)
        if value > 0:
            long_total += weight * value
        elif value < 0:
            short_total += weight * (-value)

    result.long_pressure = round(100.0 * long_total, 4)
    result.short_pressure = round(100.0 * short_total, 4)
    result.net = round(result.long_pressure - result.short_pressure, 4)
    # Both sides pulling at once. min() rather than a sum: the amount of
    # genuine disagreement is bounded by the smaller side.
    result.conflict = round(min(result.long_pressure, result.short_pressure), 4)
    return result


def describe(result: PressureResult, top: int = 3) -> str:
    """One line naming what is actually driving the score.

    A pressure number with no attribution is unusable for judging whether
    to believe it, and "82 long" backed entirely by a heuristic carrying
    6% of the weight is a different claim from "82 long" backed by the
    book and the flow."""
    if not result.contributions:
        return "No components available yet."
    ordered = sorted(result.contributions.items(), key=lambda kv: abs(kv[1]), reverse=True)
    parts = [f"{name} {value:+.3f}" for name, value in ordered[:top] if value]
    if not parts:
        return "Every component is neutral."
    side = "long" if result.net >= 0 else "short"
    return f"{side} {abs(result.net):.0f} driven by " + ", ".join(parts)
