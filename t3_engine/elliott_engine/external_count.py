"""Server-side validation of a wave count proposed by an EXTERNAL source.

An external model (see ai_advisor/advisor.py) can suggest a count over the
whole loaded history, which is something the deterministic engine
deliberately does not do - it only ever anchors on recent pivots. That
suggestion is useful, but it is also completely untrusted: a language
model will happily return pivot indices that do not exist, waves that run
backwards in time, or a "1-2-3-4-5" whose wave 4 sits deep inside wave 1.

Nothing proposed here reaches a chart or a trade until it has been rebuilt
from THIS server's own confirmed pivots and re-checked against the same
hard Elliott rules the internal engine uses. The external side chooses
WHICH pivots to connect; it never gets to decide what is a legal wave.

The checks, in order (all of them structural - none of them "does this
look plausible"):

  1. Shape: a non-empty list of at most 8 legs, each naming a start and
     end pivot index.
  2. Labels: exactly the canonical prefix 1,2,3,4,5,A,B,C - no skipping,
     no reordering, no inventing labels. A-B-C only after a full 1-5.
  3. Indices: integers, in range, start < end, strictly increasing, and
     each leg starting exactly where the previous one ended (a count with
     gaps is not one structure).
  4. Causality: every referenced pivot must have been CONFIRMED at or
     before the caller's cutoff index. Without this an external count
     could quietly reintroduce lookahead that the rest of the engine is
     built to make impossible.
  5. Alternation: each leg must run from a swing of one kind to the other
     (HIGH->LOW or LOW->HIGH), and leg 1 must start on the kind the
     trend direction requires.
  6. Hard Elliott rules: the rebuilt waves go through the exact same
     build_candidate_waves() path as an internally generated count, so a
     mathematically impossible wave is rejected by the same code that
     would reject it internally - not by a second, drifting copy of the
     rules written for this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from t3_engine.common.models import Pivot, Wave, next_id
from t3_engine.common.types import Direction, Timeframe, WaveLabel, WaveStatus
from t3_engine.elliott_engine.scenario import (
    STRUCTURE_DIAGONAL_CONTRACTING,
    STRUCTURE_DIAGONAL_EXPANDING,
    STRUCTURE_IMPULSE,
    build_candidate_waves,
)

MAX_PROPOSED_WAVES = 8
CANONICAL_LABELS = [WaveLabel.W1, WaveLabel.W2, WaveLabel.W3, WaveLabel.W4, WaveLabel.W5,
                    WaveLabel.A, WaveLabel.B, WaveLabel.C]

# Corrective structures. The motive path above counts 1-5 (optionally
# followed by A-B-C as the correction OF that motive); these are the
# standalone corrections of the books - a zigzag, a flat or a triangle
# that exists in its own right, e.g. as someone else's wave 4. They need
# their own label sets because an A-B-C is NOT a 1-2-3 with different
# letters: it obeys different rules and this file must not let one be
# submitted as the other.
STRUCTURE_ZIGZAG = "ZIGZAG"
STRUCTURE_FLAT = "FLAT"
STRUCTURE_TRIANGLE = "TRIANGLE"

MOTIVE_STRUCTURES = (STRUCTURE_IMPULSE, STRUCTURE_DIAGONAL_CONTRACTING, STRUCTURE_DIAGONAL_EXPANDING)
CORRECTIVE_STRUCTURES = (STRUCTURE_ZIGZAG, STRUCTURE_FLAT, STRUCTURE_TRIANGLE)
ALL_STRUCTURES = MOTIVE_STRUCTURES + CORRECTIVE_STRUCTURES

_ABC = [WaveLabel.A, WaveLabel.B, WaveLabel.C]
_ABCDE = _ABC + [WaveLabel.D, WaveLabel.E]
LABELS_BY_STRUCTURE = {
    STRUCTURE_IMPULSE: CANONICAL_LABELS,
    STRUCTURE_DIAGONAL_CONTRACTING: CANONICAL_LABELS,
    STRUCTURE_DIAGONAL_EXPANDING: CANONICAL_LABELS,
    STRUCTURE_ZIGZAG: _ABC,
    STRUCTURE_FLAT: _ABC,
    STRUCTURE_TRIANGLE: _ABCDE,
}

# A flat is a flat because wave B retraces most of wave A. Below this the
# structure is a zigzag, whatever it is labelled (Frost & Prechter ch. 2:
# B "terminates at or near the level of the beginning of wave A"; the
# 61.8% floor is the conventional dividing line).
FLAT_MIN_B_RETRACE = 0.618


class ExternalCountRejected(Exception):
    """Raised with a human-readable reason. Callers surface the reason
    rather than silently dropping the proposal - a rejected count is
    information (the model got it wrong, and how), not just a failure."""


@dataclass
class ValidatedExternalCount:
    waves: List[Wave]
    structure: str
    broken_rule: Optional[str] = None
    notes: str = ""
    pivot_indices: List[int] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return self.broken_rule is None and bool(self.waves)


def _as_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExternalCountRejected(f"{field_name} must be a number, got {value!r}")
    as_int = int(value)
    if as_int != value:
        raise ExternalCountRejected(f"{field_name} must be a whole pivot index, got {value!r}")
    return as_int


def _expected_start_kind(direction: Direction) -> str:
    return "LOW" if direction == Direction.UP else "HIGH"


def parse_proposed_legs(proposed: Any,
                        labels: Optional[List[WaveLabel]] = None) -> List[Dict[str, int]]:
    """Normalize whatever the external side sent into leg dicts, rejecting
    anything structurally malformed before it can reach the rule checks.

    `labels` is the canonical label sequence the structure must follow -
    1-5-A-B-C for a motive count, A-B-C for a zigzag or flat, A-B-C-D-E
    for a triangle. Positional, always: the external side does not get to
    choose which label goes where, only where the structure starts and
    ends."""
    labels = labels if labels is not None else CANONICAL_LABELS
    if not isinstance(proposed, list) or not proposed:
        raise ExternalCountRejected("Proposed count must be a non-empty list of waves")
    if len(proposed) > len(labels):
        raise ExternalCountRejected(
            f"Proposed count has {len(proposed)} waves; at most {len(labels)} "
            f"({'-'.join(x.value for x in labels)}) are countable here"
        )

    legs = []
    for position, raw in enumerate(proposed):
        if not isinstance(raw, dict):
            raise ExternalCountRejected(f"Wave #{position + 1} must be an object, got {raw!r}")
        label_raw = raw.get("label")
        expected = labels[position]
        if str(label_raw).strip().upper() != expected.value.upper():
            raise ExternalCountRejected(
                f"Wave #{position + 1} is labelled {label_raw!r}; a count must run in canonical order "
                f"({'-'.join(x.value for x in labels)}), so this position must be {expected.value!r}"
            )
        legs.append({
            "label": expected,
            "start": _as_int(raw.get("start_pivot_index"), f"Wave {expected.value} start_pivot_index"),
            "end": _as_int(raw.get("end_pivot_index"), f"Wave {expected.value} end_pivot_index"),
        })
    return legs


def _structural_checks(pivots: List[Pivot], legs: List[Dict[str, int]],
                       direction: Optional[Direction],
                       max_confirmed_index: Optional[int]) -> None:
    """Everything that must hold for ANY structure, motive or corrective,
    before a single Elliott rule is even consulted. Raises on the first
    failure - these are not "weak count" findings, they are "that is not a
    count" findings.

    `direction` may be None for a standalone correction, whose own
    direction is read off wave A rather than imposed by a larger trend."""
    for leg in legs:
        for edge in ("start", "end"):
            index = leg[edge]
            if not 0 <= index < len(pivots):
                raise ExternalCountRejected(
                    f"Wave {leg['label'].value} references pivot index {index}, which does not exist "
                    f"(history has {len(pivots)} confirmed pivots)"
                )
        if leg["start"] >= leg["end"]:
            raise ExternalCountRejected(
                f"Wave {leg['label'].value} runs from pivot {leg['start']} to {leg['end']} - a wave must move forward in time"
            )

    for previous, current in zip(legs, legs[1:]):
        if current["start"] != previous["end"]:
            raise ExternalCountRejected(
                f"Wave {current['label'].value} starts at pivot {current['start']} but wave "
                f"{previous['label'].value} ended at pivot {previous['end']} - a count is one continuous structure, not disjoint legs"
            )

    if max_confirmed_index is not None:
        for leg in legs:
            for edge in ("start", "end"):
                pivot = pivots[leg[edge]]
                if pivot.confirmed_at_index > max_confirmed_index:
                    raise ExternalCountRejected(
                        f"Wave {leg['label'].value} references pivot {leg[edge]}, only confirmed at candle "
                        f"{pivot.confirmed_at_index} - past the cutoff at candle {max_confirmed_index}. "
                        "A count may not be built from swings that had not happened yet."
                    )

    if direction is not None:
        expected_kind = _expected_start_kind(direction)
        if pivots[legs[0]["start"]].kind != expected_kind:
            raise ExternalCountRejected(
                f"A {direction.value} count must start at a {expected_kind} swing, but pivot "
                f"{legs[0]['start']} is a {pivots[legs[0]['start']].kind}"
            )

    for leg in legs:
        start_kind, end_kind = pivots[leg["start"]].kind, pivots[leg["end"]].kind
        if start_kind == end_kind:
            raise ExternalCountRejected(
                f"Wave {leg['label'].value} runs {start_kind} -> {end_kind}; a wave always connects a swing high to a swing low or vice versa"
            )


def validate_external_count(pivots: List[Pivot], proposed: Any, direction: Direction,
                            degree: Timeframe, max_confirmed_index: Optional[int] = None,
                            structure: str = STRUCTURE_IMPULSE) -> ValidatedExternalCount:
    """Rebuild `proposed` from `pivots` and re-run the engine's own hard
    rules over it. Raises ExternalCountRejected for anything structurally
    impossible; returns a result whose `broken_rule` is set when the count
    is well-formed but breaks an Elliott rule (a real answer: "you can
    connect those pivots, but that is not a legal impulse").

    This is the MOTIVE path (1-2-3-4-5, optionally plus the A-B-C that
    corrects it). For a standalone zigzag, flat or triangle, go through
    validate_external_structure() instead - it dispatches here for motive
    structures and to the corrective rules otherwise."""
    legs = parse_proposed_legs(proposed, LABELS_BY_STRUCTURE.get(structure, CANONICAL_LABELS))
    _structural_checks(pivots, legs, direction, max_confirmed_index)

    # Re-run the ENGINE's own rules over the engine's own pivots. The
    # external side only got to pick which pivots to connect.
    chain = [pivots[legs[0]["start"]]] + [pivots[leg["end"]] for leg in legs]
    built = build_candidate_waves(chain, direction, degree, structure=structure)
    waves = built["waves"]
    indices = [legs[0]["start"]] + [leg["end"] for leg in legs]

    if len(waves) != len(legs):
        # build_candidate_waves stops at the first broken rule, so a short
        # result means the proposal died partway through validation.
        broken = built["broken_rule"]
        return ValidatedExternalCount(
            waves=waves,
            structure=structure,
            broken_rule=broken.broken_rule if broken is not None else "INCOMPLETE_COUNT",
            notes=(broken.message if broken is not None else
                   f"Only {len(waves)} of {len(legs)} proposed waves could be built from these pivots"),
            pivot_indices=indices,
        )

    broken = built["broken_rule"]
    return ValidatedExternalCount(
        waves=waves,
        structure=structure,
        broken_rule=broken.broken_rule if broken is not None else None,
        notes=broken.message if broken is not None else "",
        pivot_indices=indices,
    )


# --------------------------------------------------------------------------
# Corrective structures
# --------------------------------------------------------------------------
# A correction is not an impulse with letters. Each of the three simple
# corrective patterns has its own defining constraint, and getting these
# wrong is the single most common way a count "looks right" while being
# nonsense - which is exactly why they are checked here, server-side,
# rather than trusted from whoever proposed the count.


def _corrective_direction(first_leg_start: Pivot) -> Direction:
    """A standalone correction's direction is the direction of wave A: a
    correction that starts at a swing HIGH is a down correction."""
    return Direction.DOWN if first_leg_start.kind == "HIGH" else Direction.UP


def _build_corrective_waves(pivots: List[Pivot], legs: List[Dict[str, int]],
                            degree: Timeframe, direction: Direction,
                            parent_wave_id: Optional[str] = None) -> List[Wave]:
    """A, C and E move WITH the correction; B and D move against it."""
    against = (WaveLabel.B, WaveLabel.D)
    waves = []
    for leg in legs:
        start, end = pivots[leg["start"]], pivots[leg["end"]]
        leg_direction = direction.opposite() if leg["label"] in against else direction
        waves.append(Wave(
            wave_id=next_id("wave"),
            parent_wave_id=parent_wave_id,
            degree=degree,
            label=leg["label"],
            direction=leg_direction,
            start_timestamp=start.timestamp,
            end_timestamp=end.timestamp,
            start_price=start.price,
            end_price=end.price,
            high=max(start.price, end.price),
            low=min(start.price, end.price),
            status=WaveStatus.CONFIRMED,
        ))
    return waves


def _check_zigzag(waves: List[Wave]) -> Optional[tuple]:
    """Zigzag (5-3-5): the sharp correction.

    Z1. Wave B never retraces the whole of wave A - it may not pass the
        start of wave A.
    Z2. Wave C ends beyond the end of wave A. A "zigzag" whose C fails to
        exceed A is not a zigzag; it is a flat or an unfinished pattern,
        and calling it a zigzag would put its target in the wrong place."""
    a, b, c = waves[0], waves[1], waves[2]
    down = a.direction == Direction.DOWN
    if (down and b.end_price > a.start_price) or (not down and b.end_price < a.start_price):
        return ("ZIGZAG_B_PASSES_START_OF_A",
                f"Wave B ends at {b.end_price:g}, beyond the start of wave A at {a.start_price:g} - "
                "wave B never retraces more than all of wave A")
    if (down and c.end_price >= a.end_price) or (not down and c.end_price <= a.end_price):
        return ("ZIGZAG_C_FAILS_TO_PASS_A",
                f"Wave C ends at {c.end_price:g}, short of the end of wave A at {a.end_price:g} - "
                "in a zigzag wave C always carries beyond the end of wave A")
    return None


def _check_flat(waves: List[Wave]) -> Optional[tuple]:
    """Flat (3-3-5): the sideways correction. What makes it a flat rather
    than a zigzag is the DEPTH of wave B - it retraces most or all of wave
    A. Both the expanded flat (B beyond the start of A) and the running
    flat (C short of the end of A) are legal here and deliberately not
    rejected; only a shallow B is."""
    a, b = waves[0], waves[1]
    if a.length == 0:
        return ("FLAT_A_HAS_NO_RANGE", "Wave A has zero price range")
    retrace = abs(b.end_price - a.end_price) / a.length
    if retrace < FLAT_MIN_B_RETRACE:
        return ("FLAT_B_TOO_SHALLOW",
                f"Wave B retraces only {retrace * 100:.1f}% of wave A; a flat needs at least "
                f"{FLAT_MIN_B_RETRACE * 100:.1f}% (below that the structure is a zigzag, not a flat)")
    return None


def _check_triangle(waves: List[Wave]) -> Optional[tuple]:
    """Triangle (3-3-3-3-3): five overlapping legs, A-B-C-D-E, that either
    contract throughout or expand throughout. A triangle whose legs do
    neither is not a triangle - it is just five legs, and the thrust
    measurement that makes triangles worth identifying would be
    meaningless."""
    lengths = [w.length for w in waves]
    if any(length == 0 for length in lengths):
        return ("TRIANGLE_LEG_HAS_NO_RANGE", "A triangle leg has zero price range")
    contracting = lengths[2] < lengths[0] and lengths[3] < lengths[1] and lengths[4] < lengths[2]
    expanding = lengths[2] > lengths[0] and lengths[3] > lengths[1] and lengths[4] > lengths[2]
    if not contracting and not expanding:
        return ("TRIANGLE_NOT_MONOTONE",
                "Legs " + ", ".join(f"{w.label.value}={w.length:g}" for w in waves) +
                " neither contract (C<A, D<B, E<C) nor expand throughout - that is not a triangle")
    return None


_CORRECTIVE_CHECKS = {
    STRUCTURE_ZIGZAG: _check_zigzag,
    STRUCTURE_FLAT: _check_flat,
    STRUCTURE_TRIANGLE: _check_triangle,
}


def validate_external_structure(pivots: List[Pivot], proposed: Any, structure: str,
                                degree: Timeframe, direction: Optional[Direction] = None,
                                max_confirmed_index: Optional[int] = None) -> ValidatedExternalCount:
    """The single entry point for an externally proposed structure of any
    kind. Motive structures go through the engine's own impulse/diagonal
    rules; corrective ones through the pattern rules above.

    For a corrective structure `direction` is IGNORED as an input
    constraint and derived from wave A instead: a standalone correction
    has no larger trend in this call's scope to be measured against, and
    pretending otherwise would reject perfectly legal counts for the wrong
    reason."""
    if structure not in ALL_STRUCTURES:
        raise ExternalCountRejected(
            f"Unknown structure {structure!r}; expected one of {', '.join(ALL_STRUCTURES)}"
        )

    if structure in MOTIVE_STRUCTURES:
        if direction is None:
            raise ExternalCountRejected(f"A {structure} count needs a trend direction to be validated against")
        return validate_external_count(pivots, proposed, direction, degree,
                                       max_confirmed_index=max_confirmed_index, structure=structure)

    labels = LABELS_BY_STRUCTURE[structure]
    legs = parse_proposed_legs(proposed, labels)
    if len(legs) != len(labels):
        raise ExternalCountRejected(
            f"A {structure} has {len(labels)} legs ({'-'.join(x.value for x in labels)}); "
            f"{len(legs)} were proposed. An incomplete correction is not a correction."
        )
    _structural_checks(pivots, legs, None, max_confirmed_index)

    own_direction = _corrective_direction(pivots[legs[0]["start"]])
    waves = _build_corrective_waves(pivots, legs, degree, own_direction)
    indices = [legs[0]["start"]] + [leg["end"] for leg in legs]

    broken = _CORRECTIVE_CHECKS[structure](waves)
    if broken is not None:
        return ValidatedExternalCount(waves=waves, structure=structure,
                                      broken_rule=broken[0], notes=broken[1], pivot_indices=indices)
    return ValidatedExternalCount(waves=waves, structure=structure, pivot_indices=indices)
