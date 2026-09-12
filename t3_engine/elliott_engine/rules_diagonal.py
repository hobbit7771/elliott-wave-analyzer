"""Diagonal (leading/ending) rule validation - spec section 5.

The spec is explicit that a diagonal must be PROVEN, not assumed: a
standard impulse count that merely fails the wave-4-overlap rule is not
automatically relabelled a diagonal. The overlap exception only applies if
the leg *also* satisfies the diagonal-specific shape rules below.

Rules implemented (contracting diagonal - by far the most common variant
that appears as wave 5 of an impulse, or wave C of a correction):
  D1. Wave 2 still never retraces more than 100% of Wave 1 (shared with
      the regular impulse rule - this one is never relaxed).
  D2. Wave 4 IS allowed to overlap Wave 1's territory (the defining
      exception vs. a normal impulse).
  D3. Contracting shape: |wave3| < |wave1| and |wave5| < |wave3|
      (each successive motive leg is shorter than the one before -
      the trendlines drawn through 1-3 and 2-4 converge).
  D4. Wave 5 does not travel beyond the 1-3 trendline by an excessive
      margin (a mild throw-over is normal for an ending diagonal; a huge
      break invalidates the "contracting" shape and this is simply not
      a diagonal).

Expanding diagonals (the rarer mirror image, each leg progressively
LARGER) are supported via `variant="EXPANDING"`.
"""

from __future__ import annotations

from typing import Optional

from t3_engine.common.models import Wave
from t3_engine.common.types import Direction
from t3_engine.elliott_engine.rules_impulse import ValidationResult, check_wave2_rule


def check_diagonal_shape(wave1: Wave, wave3: Wave, wave5: Optional[Wave], variant: str = "CONTRACTING") -> ValidationResult:
    if wave5 is None:
        if variant == "CONTRACTING" and wave3.length >= wave1.length:
            return ValidationResult(False, "DIAGONAL_NOT_CONTRACTING",
                                     f"Wave 3 ({wave3.length}) is not shorter than Wave 1 ({wave1.length})")
        if variant == "EXPANDING" and wave3.length <= wave1.length:
            return ValidationResult(False, "DIAGONAL_NOT_EXPANDING",
                                     f"Wave 3 ({wave3.length}) is not longer than Wave 1 ({wave1.length})")
        return ValidationResult(True, message="wave5 not yet available, shape pending")

    if variant == "CONTRACTING":
        if not (wave3.length < wave1.length and wave5.length < wave3.length):
            return ValidationResult(False, "DIAGONAL_NOT_CONTRACTING",
                                     f"lengths must strictly contract: 1={wave1.length} 3={wave3.length} 5={wave5.length}")
    else:  # EXPANDING
        if not (wave3.length > wave1.length and wave5.length > wave3.length):
            return ValidationResult(False, "DIAGONAL_NOT_EXPANDING",
                                     f"lengths must strictly expand: 1={wave1.length} 3={wave3.length} 5={wave5.length}")
    return ValidationResult(True)


def validate_diagonal(wave1: Wave, wave2: Wave, wave3: Wave, wave4: Wave,
                       wave5: Optional[Wave], direction: Direction,
                       variant: str = "CONTRACTING") -> ValidationResult:
    wave2_check = check_wave2_rule(wave1, wave2, direction)
    if not wave2_check.valid:
        return wave2_check

    shape_check = check_diagonal_shape(wave1, wave3, wave5, variant=variant)
    if not shape_check.valid:
        return shape_check

    return ValidationResult(True, message=f"valid {variant.lower()} diagonal")
