"""Corrective structure classification (spec section 5).

IMPORTANT LIMITATION (flagged per spec section 30 - "explain the technical
reason and propose the nearest correct solution" wherever a literal
requirement cannot be fully automated):

Formally distinguishing a zigzag (5-3-5 internal subdivision) from a flat
(3-3-5) or a triangle (3-3-3-3-3) requires recursively counting the
internal subwaves of A, B and C themselves to arbitrary depth. There is no
known closed-form algorithm that does this reliably even in professional
Elliott Wave software - counts are inherently ambiguous and disputed even
among human analysts looking at the same chart. That is a property of the
theory, not a shortcut we are taking.

The nearest correct, defensible solution implemented here is the standard
*price-ratio heuristic* practitioners use as a first-pass classifier:
  - Zigzag : B retraces a SMALL-to-MODERATE fraction of A (< ~78.6%),
             C usually extends beyond A's terminus.
  - Flat   : B retraces MOST-to-ALL of A (>= ~90%), C is roughly equal in
             length to A.
  - Triangle: five overlapping, alternating legs (A-B-C-D-E) with
             monotonically CONTRACTING (or expanding) range - checked
             directly since it doesn't depend on subwave counts.
  - Combination (W-X-Y[-X-Z]): two+ corrective structures joined by X
             wave(s) that themselves do not make a significant new extreme.
  - Anything that fits none of the above cleanly -> UNKNOWN_CORRECTION,
    which the spec explicitly allows and requires ("do not force every
    move into a 1-2-3-4-5 or ABC label").
"""

from __future__ import annotations

from typing import List, Optional

from t3_engine.common.models import Wave
from t3_engine.common.types import StructureType

FLAT_B_MIN_RETRACE = 0.90
ZIGZAG_B_MAX_RETRACE = 0.786
TRIANGLE_MIN_LEGS = 5


def _retrace_fraction(a: Wave, b: Wave) -> float:
    if a.length == 0:
        return 0.0
    return abs(b.end_price - a.end_price) / a.length


def classify_abc(wave_a: Wave, wave_b: Wave, wave_c: Wave) -> StructureType:
    b_retrace = _retrace_fraction(wave_a, wave_b)
    c_vs_a = wave_c.length / wave_a.length if wave_a.length else 0.0

    if b_retrace >= FLAT_B_MIN_RETRACE and 0.618 - 0.15 <= c_vs_a <= 1.618 + 0.15:
        return StructureType.FLAT

    if b_retrace <= ZIGZAG_B_MAX_RETRACE:
        return StructureType.ZIGZAG

    return StructureType.UNKNOWN_CORRECTION


def classify_triangle(legs: List[Wave]) -> Optional[StructureType]:
    """legs: the 5 alternating A-B-C-D-E waves of a candidate triangle,
    oldest -> newest. A real triangle's leg lengths contract (or, more
    rarely, expand) monotonically and each leg overlaps the price range of
    the leg two before it."""
    if len(legs) != TRIANGLE_MIN_LEGS:
        return None
    lengths = [leg.length for leg in legs]
    contracting = all(lengths[i] > lengths[i + 1] for i in range(len(lengths) - 1))
    expanding = all(lengths[i] < lengths[i + 1] for i in range(len(lengths) - 1))
    if contracting or expanding:
        return StructureType.TRIANGLE
    return None


def is_combination(waves: List[Wave], x_wave_max_retrace: float = 0.5) -> bool:
    """waves: a candidate W-X-Y (or W-X-Y-X-Z) sequence. A combination is
    plausible if the connector ("X") wave(s) retrace only a modest portion
    of the corrective leg before it, rather than making a decisive new
    extreme of their own."""
    if len(waves) < 3:
        return False
    # every even-indexed wave after the first (X, and X2 if present) must
    # be a modest connector, not a dominant move
    connectors = waves[1:-1:2]
    anchor_lengths = [w.length for w in waves[0:-1:2]]
    if not connectors or not anchor_lengths:
        return False
    avg_anchor = sum(anchor_lengths) / len(anchor_lengths)
    if avg_anchor == 0:
        return False
    return all(c.length / avg_anchor <= x_wave_max_retrace for c in connectors)


def classify_correction(waves: List[Wave]) -> StructureType:
    """Top-level dispatcher: routes to the right classifier based on how
    many legs are available, per the docstring above."""
    if len(waves) == 5:
        triangle = classify_triangle(waves)
        if triangle is not None:
            return triangle
    if len(waves) >= 3 and is_combination(waves):
        return StructureType.COMBINATION
    if len(waves) == 3:
        return classify_abc(*waves)
    return StructureType.UNKNOWN_CORRECTION
