"""Section 23 confidence scoring + final accept/reject gate.

Weighting (must sum to 1.0, enforced by test):
  Elliott structural validity   30%
  Price Action / BOS / CHoCH    20%
  Fibonacci                     15%
  Volume / Taker flow           10%
  Momentum                      10%
  Derivatives / OI               5%
  Order Book                     5%
  Higher-TF context               5%

HARD Elliott invalidation always overrides the score (section 23 last
line) - `evaluate_entry` checks scenario validity FIRST and short-circuits
to a rejection before the weighted score is even computed.
"""

from __future__ import annotations

from dataclasses import dataclass

from t3_engine.common.models import Scenario, Signal, next_id
from t3_engine.common.types import EntryStage, SignalDecision, TradeSide, WaveLabel, WaveStatus, TRADEABLE_WAVES


@dataclass
class ScoreBreakdown:
    elliott: float
    price_action: float
    fibonacci: float
    volume: float
    momentum: float
    derivatives: float
    orderbook: float
    higher_tf: float


DEFAULT_WEIGHTS = dict(
    elliott=0.30, price_action=0.20, fibonacci=0.15, volume=0.10,
    momentum=0.10, derivatives=0.05, orderbook=0.05, higher_tf=0.05,
)


def compute_confidence(breakdown: ScoreBreakdown, weights: dict = None) -> float:
    w = weights or DEFAULT_WEIGHTS
    total = (
        w["elliott"] * breakdown.elliott
        + w["price_action"] * breakdown.price_action
        + w["fibonacci"] * breakdown.fibonacci
        + w["volume"] * breakdown.volume
        + w["momentum"] * breakdown.momentum
        + w["derivatives"] * breakdown.derivatives
        + w["orderbook"] * breakdown.orderbook
        + w["higher_tf"] * breakdown.higher_tf
    )
    return round(total * 100, 2)


def evaluate_entry(*, symbol: str, scenario: Scenario, wave_label: WaveLabel, entry_stage: EntryStage,
                    breakdown: ScoreBreakdown, entry_zone: tuple, stop_loss: float, take_profits: list,
                    risk_reward: float, invalidation: float, now_ms: int, data_available_at: int,
                    threshold: float = 75.0) -> Signal:
    """Builds a full audit-trail Signal, accepted or rejected, per section
    16/17. Every rejection carries a concrete, specific reason - the spec
    forbids "always finding a wave" (section 22): NO_TRADE is a valid,
    logged, first-class outcome."""

    side = TRADEABLE_WAVES.get(wave_label, TradeSide.NONE)
    confidence = compute_confidence(breakdown)

    signal = Signal(
        signal_id=next_id("signal"),
        symbol=symbol,
        created_at=now_ms,
        data_available_at_signal=data_available_at,
        side=side,
        wave_label=wave_label,
        entry_stage=entry_stage,
        confidence=confidence,
        score_breakdown=breakdown.__dict__,
        entry_zone=entry_zone,
        stop_loss=stop_loss,
        take_profits=[tp.__dict__ for tp in take_profits],
        risk_reward=risk_reward,
        invalidation=invalidation,
        wave_state_at_signal={"scenario_id": scenario.scenario_id, "status": scenario.status.value,
                               "probability": scenario.probability},
        reason="",
        decision=SignalDecision.SIGNAL_REJECTED.value,
        scenario_id=scenario.scenario_id,
    )

    # HARD rule always wins, before any score is even considered.
    if scenario.status == WaveStatus.INVALIDATED:
        signal.rejection_reason = "HARD_ELLIOTT_RULE_VIOLATED: scenario invalidated"
        signal.reason = signal.rejection_reason
        return signal

    if side == TradeSide.NONE:
        signal.rejection_reason = f"WAVE_NOT_TRADEABLE: {wave_label.value} (A/B waves are observation-only per section 12)"
        signal.reason = signal.rejection_reason
        return signal

    if entry_stage not in (EntryStage.TRIGGERED, EntryStage.CONFIRMED):
        signal.rejection_reason = f"ENTRY_STAGE_NOT_ACTIONABLE: currently {entry_stage.value}, need TRIGGERED or CONFIRMED"
        signal.reason = signal.rejection_reason
        return signal

    if confidence < threshold:
        signal.rejection_reason = f"CONFIDENCE_BELOW_THRESHOLD: {confidence} < {threshold}"
        signal.reason = signal.rejection_reason
        return signal

    if risk_reward < 1.0:
        signal.rejection_reason = f"RISK_REWARD_TOO_LOW: {risk_reward}"
        signal.reason = signal.rejection_reason
        return signal

    signal.decision = SignalDecision.SIGNAL_ACCEPTED.value
    signal.reason = f"Elliott {wave_label.value} setup confirmed at {entry_stage.value}, confidence {confidence}"
    return signal
