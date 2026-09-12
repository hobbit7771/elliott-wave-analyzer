"""Wave-type-specific stop-loss / take-profit construction (spec 9-13, 15).

Every target here is a WAVE target (Fibonacci extension/retracement,
structural liquidity), never a "fixed N% TP" (section 15 explicitly bans
that). The runner leg always exits via a trailing STRUCTURAL stop managed
by position_manager, not a fixed price.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from t3_engine.common.models import TakeProfitLeg, Wave
from t3_engine.common.types import Direction
from t3_engine.fibonacci.calculator import (
    wave3_targets_from_wave2_end,
    wave4_levels,
    wave5_targets,
    wave_c_targets,
)


@dataclass
class TradePlan:
    entry_zone: tuple
    stop_loss: float
    take_profits: List[TakeProfitLeg]
    invalidation: float
    risk_reward: float


def _rr(entry: float, stop: float, first_target: float) -> float:
    risk = abs(entry - stop)
    reward = abs(first_target - entry)
    return round(reward / risk, 2) if risk else 0.0


def plan_wave3_long(wave1: Wave, wave2: Wave, entry_price: float, liquidity_level: Optional[float] = None) -> TradePlan:
    """Wave 3 LONG - the system's priority trade (section 9)."""
    stop = wave2.low
    levels = wave3_targets_from_wave2_end(wave1.start_price, wave1.end_price, wave2.end_price)
    by_ratio = {round(l.ratio, 3): l.price for l in levels}
    tp1 = liquidity_level if liquidity_level and liquidity_level > entry_price else by_ratio[1.000]
    tps = [
        TakeProfitLeg(tp1, 0.25, "TP1_liquidity_or_1.0ext"),
        TakeProfitLeg(by_ratio[1.272], 0.25, "TP2_1.272ext"),
        TakeProfitLeg(by_ratio[1.618], 0.25, "TP3_1.618ext"),
        TakeProfitLeg(by_ratio[2.000], 0.15, "TP4_2.0ext"),
        TakeProfitLeg(by_ratio[2.618], 0.10, "runner_trailing_structural_stop"),
    ]
    return TradePlan((entry_price, entry_price), stop, tps, stop, _rr(entry_price, stop, tp1))


def plan_wave4_short(wave3: Wave, entry_price: float) -> TradePlan:
    """Wave 4 SHORT - countertrend, smaller risk multiplier applied
    upstream by risk_engine. Target: 0.236-0.382 retrace of wave 3.
    Invalidation: entering wave1's territory (section 10)."""
    levels = wave4_levels(wave3.start_price, wave3.end_price)
    by_ratio = {round(l.ratio, 3): l.price for l in levels}
    stop = wave3.high * 1.001 if wave3.direction == Direction.UP else wave3.low * 0.999
    tp1 = by_ratio[0.236]
    tp2 = by_ratio[0.382]
    tps = [
        TakeProfitLeg(tp1, 0.5, "TP1_0.236_wave3"),
        TakeProfitLeg(tp2, 0.5, "TP2_0.382_wave3"),
    ]
    return TradePlan((entry_price, entry_price), stop, tps, stop, _rr(entry_price, stop, tp1))


def plan_wave5_long(wave1: Wave, wave4: Wave, entry_price: float) -> TradePlan:
    levels = wave5_targets(wave1.start_price, wave1.end_price, wave4.end_price)
    by_ratio = {round(l.ratio, 3): l.price for l in levels}
    stop = wave4.low
    tps = [
        TakeProfitLeg(by_ratio[0.618], 0.3, "TP1_0.618xW1"),
        TakeProfitLeg(by_ratio[1.000], 0.4, "TP2_equalW1"),
        TakeProfitLeg(by_ratio[1.618], 0.3, "runner_trailing_structural_stop"),
    ]
    return TradePlan((entry_price, entry_price), stop, tps, stop, _rr(entry_price, stop, tps[0].price))


def plan_wave_c_short(wave_a: Wave, wave_b: Wave, entry_price: float) -> TradePlan:
    levels = wave_c_targets(wave_a.start_price, wave_a.end_price, wave_b.end_price)
    by_ratio = {round(l.ratio, 3): l.price for l in levels}
    stop = wave_b.high
    tps = [
        TakeProfitLeg(by_ratio[0.618], 0.25, "TP1_0.618A"),
        TakeProfitLeg(by_ratio[1.000], 0.35, "TP2_1.0A"),
        TakeProfitLeg(by_ratio[1.272], 0.25, "TP3_1.272A"),
        TakeProfitLeg(by_ratio[1.618], 0.15, "runner_trailing_structural_stop"),
    ]
    return TradePlan((entry_price, entry_price), stop, tps, stop, _rr(entry_price, stop, tps[0].price))
