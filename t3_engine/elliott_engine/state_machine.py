"""Section 8 state machine.

The state machine encodes the sequential Elliott lifecycle so the engine
can never "skip" from, say, SEARCHING_W1 straight to W3_ACTIVE without
structural justification (a confirmed Wave 1, then a confirmed Wave 2).
Every edge in `_TRANSITIONS` is a state transition the spec explicitly
describes; anything not listed is refused.

From ANY state the machine may fall back to SEARCHING_W1 - that is what
"scenario INVALIDATED" means in practice: the hard Elliott rules broke, so
whatever wave count we were building is thrown out and we start over.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, Set


class WaveState(str, Enum):
    SEARCHING_W1 = "SEARCHING_W1"
    W1_DEVELOPING = "W1_DEVELOPING"
    W1_CONFIRMED = "W1_CONFIRMED"

    W2_DEVELOPING = "W2_DEVELOPING"
    W2_TERMINATION_ZONE = "W2_TERMINATION_ZONE"
    W2_CONFIRMED = "W2_CONFIRMED"

    W3_TRIGGER_ARMED = "W3_TRIGGER_ARMED"
    W3_ACTIVE = "W3_ACTIVE"
    W3_EXTENDING = "W3_EXTENDING"
    W3_COMPLETING = "W3_COMPLETING"

    W4_DEVELOPING = "W4_DEVELOPING"
    W4_SHORT_ARMED = "W4_SHORT_ARMED"
    W4_ACTIVE = "W4_ACTIVE"
    W4_COMPLETING = "W4_COMPLETING"

    W5_TRIGGER_ARMED = "W5_TRIGGER_ARMED"
    W5_ACTIVE = "W5_ACTIVE"
    W5_EXHAUSTION = "W5_EXHAUSTION"

    ABC_PENDING = "ABC_PENDING"
    A_ACTIVE = "A_ACTIVE"
    B_ACTIVE = "B_ACTIVE"
    C_SHORT_ARMED = "C_SHORT_ARMED"
    C_ACTIVE = "C_ACTIVE"
    C_COMPLETING = "C_COMPLETING"


_FORWARD_PATH = [
    WaveState.SEARCHING_W1,
    WaveState.W1_DEVELOPING,
    WaveState.W1_CONFIRMED,
    WaveState.W2_DEVELOPING,
    WaveState.W2_TERMINATION_ZONE,
    WaveState.W2_CONFIRMED,
    WaveState.W3_TRIGGER_ARMED,
    WaveState.W3_ACTIVE,
    WaveState.W3_EXTENDING,
    WaveState.W3_COMPLETING,
    WaveState.W4_DEVELOPING,
    WaveState.W4_SHORT_ARMED,
    WaveState.W4_ACTIVE,
    WaveState.W4_COMPLETING,
    WaveState.W5_TRIGGER_ARMED,
    WaveState.W5_ACTIVE,
    WaveState.W5_EXHAUSTION,
    WaveState.ABC_PENDING,
    WaveState.A_ACTIVE,
    WaveState.B_ACTIVE,
    WaveState.C_SHORT_ARMED,
    WaveState.C_ACTIVE,
    WaveState.C_COMPLETING,
]


def _build_transition_table() -> Dict[WaveState, Set[WaveState]]:
    table: Dict[WaveState, Set[WaveState]] = {s: set() for s in WaveState}
    for i in range(len(_FORWARD_PATH) - 1):
        table[_FORWARD_PATH[i]].add(_FORWARD_PATH[i + 1])
    # W3_EXTENDING can loop back onto itself (an extended wave 3 keeps
    # extending across many candles before it completes).
    table[WaveState.W3_EXTENDING].add(WaveState.W3_EXTENDING)
    # W5 can truncate straight from ACTIVE to EXHAUSTION->ABC without a
    # separate "extending" step (5th waves don't get their own extending
    # state in the spec - only 3rd waves do).
    # After C_COMPLETING, a brand new cycle begins.
    table[WaveState.C_COMPLETING].add(WaveState.SEARCHING_W1)
    # Every state can fall back to SEARCHING_W1 - a hard-rule invalidation
    # can happen at any point in the lifecycle.
    for s in WaveState:
        table[s].add(WaveState.SEARCHING_W1)
    return table


_TRANSITIONS = _build_transition_table()


class IllegalStateTransition(Exception):
    pass


class ElliottStateMachine:
    def __init__(self, initial: WaveState = WaveState.SEARCHING_W1):
        self.state = initial
        self.history = [initial]

    def can_transition(self, target: WaveState) -> bool:
        return target in _TRANSITIONS[self.state]

    def transition(self, target: WaveState, reason: str = "") -> bool:
        if not self.can_transition(target):
            raise IllegalStateTransition(
                f"Cannot jump {self.state.value} -> {target.value} without structural cause "
                f"(reason given: '{reason or 'none'}')"
            )
        self.state = target
        self.history.append(target)
        return True

    def invalidate(self, reason: str = "hard rule violation") -> None:
        self.state = WaveState.SEARCHING_W1
        self.history.append(WaveState.SEARCHING_W1)
