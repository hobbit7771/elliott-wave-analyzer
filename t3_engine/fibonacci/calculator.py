"""Fibonacci relationship calculations (spec section 6).

These functions are deliberately pure and side-effect free: Fibonacci is
NOT a standalone signal in this system, it is one input the scenario
engine uses to score how likely a given Elliott hypothesis is. Nothing
here decides to trade on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


WAVE2_RATIOS = [0.382, 0.500, 0.618, 0.786, 0.886]
WAVE3_RATIOS = [1.000, 1.272, 1.618, 2.000, 2.618]
WAVE4_RATIOS = [0.236, 0.382, 0.500]
WAVE5_RATIOS_VS_WAVE1 = [0.618, 1.000, 1.272, 1.618]
WAVE_C_RATIOS_VS_A = [0.618, 1.000, 1.272, 1.618]

# How close (as a fraction of the reference leg's length) a price needs to
# land to a target ratio to be considered "at" that level.
DEFAULT_TOLERANCE = 0.06


@dataclass
class FibLevel:
    ratio: float
    price: float


def retracement_levels(start_price: float, end_price: float, ratios: List[float]) -> List[FibLevel]:
    """Retracement levels of the move start_price -> end_price."""
    length = end_price - start_price
    return [FibLevel(r, end_price - length * r) for r in ratios]


def extension_levels(start_price: float, end_price: float, ratios: List[float]) -> List[FibLevel]:
    """Extension levels projected in the same direction as start->end,
    anchored at end_price (used for wave 3/5/C targets)."""
    length = end_price - start_price
    return [FibLevel(r, end_price + length * r) for r in ratios]


def wave2_levels(wave1_start: float, wave1_end: float) -> List[FibLevel]:
    return retracement_levels(wave1_start, wave1_end, WAVE2_RATIOS)


def wave3_targets(wave1_start: float, wave1_end: float) -> List[FibLevel]:
    """Wave 3 length projected from the END of wave 2 in the direction of
    wave 1. Caller supplies wave2_end as the anchor via `anchor` override
    since wave3 projects off wave2's terminus, not wave1's."""
    return extension_levels(wave1_start, wave1_end, WAVE3_RATIOS)


def wave3_targets_from_wave2_end(wave1_start: float, wave1_end: float, wave2_end: float) -> List[FibLevel]:
    length = wave1_end - wave1_start
    return [FibLevel(r, wave2_end + length * r) for r in WAVE3_RATIOS]


def wave4_levels(wave3_start: float, wave3_end: float) -> List[FibLevel]:
    return retracement_levels(wave3_start, wave3_end, WAVE4_RATIOS)


def wave5_targets(wave1_start: float, wave1_end: float, wave4_end: float) -> List[FibLevel]:
    length = wave1_end - wave1_start
    return [FibLevel(r, wave4_end + length * r) for r in WAVE5_RATIOS_VS_WAVE1]


def wave_c_targets(wave_a_start: float, wave_a_end: float, wave_b_end: float) -> List[FibLevel]:
    length = wave_a_end - wave_a_start
    return [FibLevel(r, wave_b_end + length * r) for r in WAVE_C_RATIOS_VS_A]


def nearest_ratio_score(price: float, levels: List[FibLevel], reference_length: float, tolerance: float = DEFAULT_TOLERANCE) -> float:
    """0..1 score: how close `price` is to the nearest fib level, relative
    to the reference leg length. Used by the scenario engine's fib_score."""
    if reference_length == 0 or not levels:
        return 0.0
    closest = min(abs(price - lvl.price) for lvl in levels)
    distance_frac = closest / abs(reference_length)
    if distance_frac >= tolerance:
        return 0.0
    return 1.0 - (distance_frac / tolerance)


def support_resistance_levels(high: float, low: float) -> Dict[str, float]:
    """Generic retracement grid between an arbitrary high/low. NOT
    currently called anywhere in this codebase - the dashboard's actual
    Fibonacci overlay (dashboard/server.py's fibonacci_levels_for_scenario)
    uses the wave-specific functions above instead, since those are the
    same ones signal_engine/targets.py uses to build real TP/SL, so the
    chart shows exactly what the strategy itself is measuring rather than
    a generic grid that could disagree with it. Kept here as a
    general-purpose building block, not dead code to delete, but an
    earlier version of this docstring wrongly claimed the dashboard was
    already using it - it wasn't."""
    diff = high - low
    ratios = {
        "0.0": high,
        "0.236": high - diff * 0.236,
        "0.382": high - diff * 0.382,
        "0.5": high - diff * 0.5,
        "0.618": high - diff * 0.618,
        "0.786": high - diff * 0.786,
        "1.0": low,
    }
    return ratios
