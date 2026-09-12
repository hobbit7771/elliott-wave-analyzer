"""Server-side validation of a wave count proposed by an EXTERNAL source.

An external model (see ai_advisor/gemini.py) can suggest a count over the
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

from t3_engine.common.models import Pivot, Wave
from t3_engine.common.types import Direction, Timeframe, WaveLabel
from t3_engine.elliott_engine.scenario import (
    STRUCTURE_IMPULSE,
    build_candidate_waves,
)

MAX_PROPOSED_WAVES = 8
CANONICAL_LABELS = [WaveLabel.W1, WaveLabel.W2, WaveLabel.W3, WaveLabel.W4, WaveLabel.W5,
                    WaveLabel.A, WaveLabel.B, WaveLabel.C]


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


def parse_proposed_legs(proposed: Any) -> List[Dict[str, int]]:
    """Normalize whatever the external side sent into leg dicts, rejecting
    anything structurally malformed before it can reach the rule checks."""
    if not isinstance(proposed, list) or not proposed:
        raise ExternalCountRejected("Proposed count must be a non-empty list of waves")
    if len(proposed) > MAX_PROPOSED_WAVES:
        raise ExternalCountRejected(
            f"Proposed count has {len(proposed)} waves; at most {MAX_PROPOSED_WAVES} (1-5 plus A-B-C) are countable here"
        )

    legs = []
    for position, raw in enumerate(proposed):
        if not isinstance(raw, dict):
            raise ExternalCountRejected(f"Wave #{position + 1} must be an object, got {raw!r}")
        label_raw = raw.get("label")
        expected = CANONICAL_LABELS[position]
        if str(label_raw).strip().upper() != expected.value.upper():
            raise ExternalCountRejected(
                f"Wave #{position + 1} is labelled {label_raw!r}; a count must run in canonical order "
                f"({'-'.join(x.value for x in CANONICAL_LABELS)}), so this position must be {expected.value!r}"
            )
        legs.append({
            "label": expected,
            "start": _as_int(raw.get("start_pivot_index"), f"Wave {expected.value} start_pivot_index"),
            "end": _as_int(raw.get("end_pivot_index"), f"Wave {expected.value} end_pivot_index"),
        })
    return legs


def validate_external_count(pivots: List[Pivot], proposed: Any, direction: Direction,
                            degree: Timeframe, max_confirmed_index: Optional[int] = None,
                            structure: str = STRUCTURE_IMPULSE) -> ValidatedExternalCount:
    """Rebuild `proposed` from `pivots` and re-run the engine's own hard
    rules over it. Raises ExternalCountRejected for anything structurally
    impossible; returns a result whose `broken_rule` is set when the count
    is well-formed but breaks an Elliott rule (a real answer: "you can
    connect those pivots, but that is not a legal impulse")."""
    legs = parse_proposed_legs(proposed)

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

    # Re-run the ENGINE's own rules over the engine's own pivots. The
    # external side only got to pick which pivots to connect.
    chain = [pivots[legs[0]["start"]]] + [pivots[leg["end"]] for leg in legs]
    built = build_candidate_waves(chain, direction, degree, structure=structure)
    waves = built["waves"]

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
            pivot_indices=[legs[0]["start"]] + [leg["end"] for leg in legs],
        )

    broken = built["broken_rule"]
    return ValidatedExternalCount(
        waves=waves,
        structure=structure,
        broken_rule=broken.broken_rule if broken is not None else None,
        notes=broken.message if broken is not None else "",
        pivot_indices=[legs[0]["start"]] + [leg["end"] for leg in legs],
    )
