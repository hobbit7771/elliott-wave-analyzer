"""How often price has actually reached a target like this one.

The dashboard is about to print percentages next to Fibonacci targets, and
there are two ways to produce those. One is to ask the model, which yields
a confident number with nothing behind it - the single most dangerous
artefact this project could ship, because a percentage reads as measurement
even when it is invention.

The other is to measure. Every completed swing in the loaded history
extended some fraction of the swing before it. That distribution is the
chart's own answer to "how far do moves here usually carry", and the share
of past swings that reached at least ratio R is a base rate for a target
placed at R. It is not a forecast - it takes no account of where the count
says we are - which is exactly why it is worth showing next to a forecast.

Stated plainly wherever it is displayed: a base rate from THIS chart's
swings, sample size included, so a number computed from nine swings is not
mistaken for one computed from four hundred.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from t3_engine.common.models import Candle
from t3_engine.market_structure.pivots import ZigZagPivotDetector

# Below this there is no distribution, only anecdotes.
MIN_SWINGS_FOR_ODDS = 12


def swing_extension_ratios(candles: List[Candle], deviation_pct: float = 1.0) -> List[float]:
    """Each swing as a multiple of the swing before it."""
    detector = ZigZagPivotDetector(deviation_pct=deviation_pct)
    for index, candle in enumerate(candles):
        detector.update(index, candle)
    pivots = detector.pivots
    lengths = [abs(later.price - earlier.price) for earlier, later in zip(pivots, pivots[1:])]
    return [later / earlier for earlier, later in zip(lengths, lengths[1:]) if earlier > 0]


def odds_for_ratios(candles: List[Candle], ratios: List[float],
                    deviation_pct: float = 1.0) -> Optional[Dict[str, Any]]:
    """Share of past swings that reached at least each ratio.

    Returns None rather than a number when the history is too short - a
    percentage from nine swings would be believed exactly as much as one
    from nine hundred, which is the whole problem."""
    extensions = swing_extension_ratios(candles, deviation_pct)
    if len(extensions) < MIN_SWINGS_FOR_ODDS:
        return None

    total = len(extensions)
    rows = []
    for ratio in ratios:
        reached = sum(1 for extension in extensions if extension >= ratio)
        rows.append({
            "ratio": round(float(ratio), 3),
            "probability": round(reached / total, 3),
            "reached": reached,
        })
    ordered = sorted(extensions)
    return {
        "sample_size": total,
        "deviation_pct": deviation_pct,
        "median_extension": round(ordered[total // 2], 3),
        "rows": rows,
        "basis": (f"Base rate from this chart's own {total} completed swings: the share that "
                  "extended at least this far relative to the swing before them. Not a forecast - "
                  "it knows nothing about where the count says price is now."),
    }


def annotate_projection(projection: Optional[Dict[str, Any]], candles: List[Candle],
                        deviation_pct: float = 1.0) -> Optional[Dict[str, Any]]:
    """Attach a measured base rate to each target of a projection."""
    if not projection or not projection.get("targets"):
        return projection
    ratios = [float(target.get("ratio", 0)) for target in projection["targets"]]
    odds = odds_for_ratios(candles, ratios, deviation_pct)
    if odds is None:
        annotated = dict(projection)
        annotated["odds_note"] = (
            f"Fewer than {MIN_SWINGS_FOR_ODDS} completed swings in this history - too few for a "
            "base rate worth printing, so none is shown."
        )
        return annotated

    by_ratio = {row["ratio"]: row for row in odds["rows"]}
    annotated = dict(projection)
    annotated["targets"] = [
        {**target, "probability": by_ratio.get(round(float(target.get("ratio", 0)), 3), {}).get("probability")}
        for target in projection["targets"]
    ]
    annotated["odds"] = odds
    return annotated
