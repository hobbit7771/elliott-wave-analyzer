"""Hard, non-negotiable Elliott impulse rules (spec section 5).

These three rules are the only things that can zero out a scenario's
probability outright (section 7: "if a scenario violates a HARD rule,
probability = 0, INVALIDATED"). Everything else in the system is scored,
weighted, and probabilistic - these are not.

    Rule 1: Wave 2 never retraces more than 100% of Wave 1
            (it may never cross back past Wave 1's own starting price).
    Rule 2: Wave 3 is never the shortest wave among 1, 3 and 5.
    Rule 3: Wave 4 never enters the price territory of Wave 1
            (no overlap) - UNLESS the structure is a diagonal
            (see rules_diagonal.py), which has its own separate rule set.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from t3_engine.common.models import Wave
from t3_engine.common.types import Direction


@dataclass
class ValidationResult:
    valid: bool
    broken_rule: Optional[str] = None
    message: str = ""


def check_wave2_rule(wave1: Wave, wave2: Wave, direction: Direction) -> ValidationResult:
    if direction == Direction.UP:
        if wave2.end_price <= wave1.start_price:
            return ValidationResult(False, "WAVE2_OVER_100_PCT",
                                     f"Wave 2 end {wave2.end_price} retraced past Wave 1 start {wave1.start_price}")
    else:
        if wave2.end_price >= wave1.start_price:
            return ValidationResult(False, "WAVE2_OVER_100_PCT",
                                     f"Wave 2 end {wave2.end_price} retraced past Wave 1 start {wave1.start_price}")
    return ValidationResult(True)


def check_wave3_not_shortest(wave1: Wave, wave3: Wave, wave5: Optional[Wave]) -> ValidationResult:
    if wave5 is None:
        # Wave 5 hasn't formed yet - nothing to compare against, rule can't
        # be violated (or confirmed) yet. Not a failure.
        return ValidationResult(True, message="wave5 not yet available, rule pending")
    lengths = {"1": wave1.length, "3": wave3.length, "5": wave5.length}
    shortest = min(lengths, key=lengths.get)
    if shortest == "3":
        return ValidationResult(False, "WAVE3_SHORTEST",
                                 f"Wave 3 length {wave3.length} is the shortest of 1/3/5: {lengths}")
    return ValidationResult(True)


def check_wave4_overlap(wave1: Wave, wave4: Wave, direction: Direction, allow_overlap: bool = False) -> ValidationResult:
    if allow_overlap:
        return ValidationResult(True, message="overlap permitted (diagonal)")
    if direction == Direction.UP:
        if wave4.low <= wave1.high:
            return ValidationResult(False, "WAVE4_OVERLAPS_WAVE1",
                                     f"Wave 4 low {wave4.low} entered Wave 1 territory (Wave1 high {wave1.high})")
    else:
        if wave4.high >= wave1.low:
            return ValidationResult(False, "WAVE4_OVERLAPS_WAVE1",
                                     f"Wave 4 high {wave4.high} entered Wave 1 territory (Wave1 low {wave1.low})")
    return ValidationResult(True)


def validate_impulse(wave1: Wave, wave2: Wave, wave3: Wave, wave4: Wave,
                      wave5: Optional[Wave], direction: Direction,
                      allow_diagonal_overlap: bool = False) -> ValidationResult:
    """Runs all three hard rules in order and returns the first failure,
    or a pass. Order matters for a useful error message but all three are
    always independently authoritative - a scenario is INVALIDATED the
    moment any one of them fails, regardless of how good its Fibonacci or
    volume scores look."""
    for check in (
        check_wave2_rule(wave1, wave2, direction),
        check_wave3_not_shortest(wave1, wave3, wave5),
        check_wave4_overlap(wave1, wave4, direction, allow_overlap=allow_diagonal_overlap),
    ):
        if not check.valid:
            return check
    return ValidationResult(True)
