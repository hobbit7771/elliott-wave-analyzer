"""Section 21: PREDICTION -> SETUP -> ARMED -> TRIGGERED -> CONFIRMED.

The whole point of watching 1s-3m data (section 3/21) is to reach
TRIGGERED as close as possible to the true start of the next wave, instead
of waiting for the higher timeframe to make it obvious after the move is
half over. This tracker enforces the same "no skipping stages" discipline
as the wave state machine.
"""

from __future__ import annotations

from typing import Optional

from t3_engine.common.types import EntryStage

_ORDER = [EntryStage.NONE, EntryStage.PREDICTION, EntryStage.SETUP,
          EntryStage.ARMED, EntryStage.TRIGGERED, EntryStage.CONFIRMED]


class IllegalStageTransition(Exception):
    pass


class EntryTimingTracker:
    def __init__(self):
        self.stage = EntryStage.NONE
        self.reason: Optional[str] = None

    def advance(self, target: EntryStage, reason: str) -> bool:
        current_idx = _ORDER.index(self.stage)
        target_idx = _ORDER.index(target)
        if target_idx != current_idx + 1:
            raise IllegalStageTransition(
                f"Cannot jump entry stage {self.stage.value} -> {target.value}; "
                f"stages must advance one at a time with a structural reason (got: '{reason}')"
            )
        self.stage = target
        self.reason = reason
        return True

    def reset(self, reason: str = "invalidated") -> None:
        self.stage = EntryStage.NONE
        self.reason = reason

    @property
    def is_actionable(self) -> bool:
        """Entry should be taken at TRIGGERED (section 21: "предпочтительно
        на стадии TRIGGERED, а не ждать... становится очевидна"), CONFIRMED
        is also acceptable (higher-TF structure has caught up)."""
        return self.stage in (EntryStage.TRIGGERED, EntryStage.CONFIRMED)
