"""The signal state machine, and nothing else.

Ten states, listed by the specification. What matters about them is that
they are ORDERED by how much is being claimed - IDLE claims nothing,
A_PLUS claims a great deal - and that the transitions are explicit rather
than derived from whichever threshold happens to be crossed first.

Two rules shape the whole thing:

  DATA_FAILURE wins over everything. A degraded feed does not produce a
  cautious signal, it produces no signal, because every number feeding the
  machine is suspect at exactly the moment it would be most tempting to
  act on one. The health monitor decides this, not the scores.

  INVALIDATED is a state, not a return to IDLE. A setup that was building
  and then broke down is a different thing from a market that was never
  interesting, and collapsing the two loses the only record that the
  engine was wrong. It decays to IDLE after a cooldown.

These states are published on the Lead Engine's own bus and rendered in
the Lead Engine's own tab. Nothing here reaches the project's signal
engine, its risk engine or its execution path - see engine.py for the
boundary.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from t3_engine.lead_engine.config import Thresholds

IDLE = "IDLE"
WATCH = "WATCH"
PRE_SIGNAL = "PRE_SIGNAL"
PRE_BREAK_LONG = "PRE_BREAK_LONG"
PRE_BREAK_SHORT = "PRE_BREAK_SHORT"
HIGH_PROBABILITY = "HIGH_PROBABILITY"
A_PLUS = "A_PLUS"
REVERSAL_CANDIDATE = "REVERSAL_CANDIDATE"
INVALIDATED = "INVALIDATED"
DATA_FAILURE = "DATA_FAILURE"

ALL_STATES = (IDLE, WATCH, PRE_SIGNAL, PRE_BREAK_LONG, PRE_BREAK_SHORT,
              HIGH_PROBABILITY, A_PLUS, REVERSAL_CANDIDATE, INVALIDATED, DATA_FAILURE)

# Pressure at which the engine starts paying attention, and at which it
# considers a setup to be forming. Below WATCH nothing is claimed at all.
WATCH_PRESSURE = 35.0
PRE_SIGNAL_PRESSURE = 55.0

# Both sides pulling this hard at once means the tape is being fought
# over; no directional state is offered through it.
MAX_CONFLICT = 30.0

# How long INVALIDATED is held before decaying back to IDLE.
INVALIDATION_COOLDOWN_SECONDS = 60.0

# Conflict levels, mirrored from layers.py so this module does not have to
# import it just for three strings.
CONFLICT_HIGH = "CONFLICT_HIGH"
CONFLICT_MEDIUM = "CONFLICT_MEDIUM"
CONFLICT_LOW = "CONFLICT_LOW"

# A reversal candidate needs a liquidation flush that has EXHAUSTED and
# pressure now leaning the other way by at least this much.
REVERSAL_PRESSURE = 30.0

# The confidence floor for a DIRECTIONAL state.
#
# Confidence is the share of each layer's own inputs that were actually
# available (see layers._combine), damped by cross-layer conflict. A
# score of 70 built from one of five flow inputs and nothing else is not
# the same claim as a score of 70 with all five, and the first build
# offered both as PRE_BREAK. Below this floor the machine may still say
# WATCH - "something is building" is an honest thing to say on partial
# data - but it may not name a side.
MIN_DIRECTIONAL_CONFIDENCE = 0.45


@dataclass
class SignalSnapshot:
    symbol: str
    state: str = IDLE
    previous_state: str = IDLE
    direction: str = ""
    confidence: float = 0.0
    reason: str = ""
    changed_at: float = field(default_factory=time.time)
    break_probability: float = 0.0
    level: Optional[float] = None

    def as_dict(self) -> Dict[str, object]:
        return {
            "symbol": self.symbol, "state": self.state,
            "previous_state": self.previous_state, "direction": self.direction,
            "confidence": round(self.confidence, 2), "reason": self.reason,
            "changed_at": self.changed_at,
            "break_probability": round(self.break_probability, 2),
            "level": self.level,
        }


@dataclass
class SignalInputs:
    long_pressure: float
    short_pressure: float
    conflict: float
    prebreak_long: float
    prebreak_short: float
    long_level: Optional[float]
    short_level: Optional[float]
    liquidation_state: str
    healthy: bool
    health_reason: str = ""
    # From layers.detect_conflict: which INDEPENDENT sources disagree,
    # rather than the old proxy of "both sides scored something". A high
    # conflict caps what the machine is allowed to claim - see _cap_state.
    conflict_level: str = "CONFLICT_LOW"
    confidence: float = 1.0


class SignalMachine:
    def __init__(self, symbol: str, thresholds: Optional[Thresholds] = None) -> None:
        self.symbol = symbol.upper()
        self.thresholds = thresholds or Thresholds()
        self.current = SignalSnapshot(symbol=self.symbol)
        self.history: List[SignalSnapshot] = []

    # ---- transition ----

    def update(self, inputs: SignalInputs, now: Optional[float] = None) -> SignalSnapshot:
        now = time.time() if now is None else now
        state, direction, confidence, reason, probability, level = self._decide(inputs, now)
        if state != self.current.state:
            snapshot = SignalSnapshot(
                symbol=self.symbol, state=state, previous_state=self.current.state,
                direction=direction, confidence=confidence, reason=reason,
                changed_at=now, break_probability=probability, level=level,
            )
            self.history.append(snapshot)
            if len(self.history) > 200:
                self.history.pop(0)
            self.current = snapshot
        else:
            # Same state, refreshed numbers. The timestamp deliberately
            # does NOT move: "in PRE_BREAK_SHORT for 40 seconds" is a fact
            # about the setup, and refreshing it every tick would erase it.
            self.current.direction = direction
            self.current.confidence = confidence
            self.current.reason = reason
            self.current.break_probability = probability
            self.current.level = level
        return self.current

    def _blocked(self, inputs: SignalInputs) -> str:
        """Why no side may be named, or "" when one may.

        Three separate reasons, kept apart because they mean different
        things and a trader should be told which one applies."""
        if inputs.conflict >= MAX_CONFLICT:
            return f"both sides pressing at once (conflict {inputs.conflict:.0f})"
        # Independent layers disagreeing is a harder stop than both sides
        # merely scoring. Structure bullish against flow and book bearish
        # must not produce a strong LONG however high the raw numbers are.
        if inputs.conflict_level == CONFLICT_HIGH:
            return (f"independent layers disagree ({inputs.conflict_level}); "
                    "no directional call")
        # Not enough of the engine answered. WATCH is still allowed -
        # noticing something on partial data is honest - but a direction
        # on it is not.
        if inputs.confidence < MIN_DIRECTIONAL_CONFIDENCE:
            return (f"confidence {inputs.confidence:.2f} below "
                    f"{MIN_DIRECTIONAL_CONFIDENCE:.2f}: too few inputs to call a side")
        return ""

    def _decide(self, inputs: SignalInputs, now: float):
        thresholds = self.thresholds
        if not inputs.healthy:
            return (DATA_FAILURE, "", 0.0,
                    inputs.health_reason or "data feed degraded", 0.0, None)

        strongest = max(inputs.prebreak_long, inputs.prebreak_short)
        # Which way the machine is looking. The pre-break probabilities
        # decide it when either of them is saying anything; when both are
        # silent - no level under stress - the side comes from pressure
        # instead. Deciding it on the probabilities alone left a market
        # with 60 short pressure and no level nearby being described as
        # "long", because zero is not less than zero.
        if max(inputs.prebreak_long, inputs.prebreak_short) > 0:
            leans_long = inputs.prebreak_long >= inputs.prebreak_short
        else:
            leans_long = inputs.long_pressure >= inputs.short_pressure
        if leans_long:
            direction, probability, level = "long", inputs.prebreak_long, inputs.long_level
            pressure = inputs.long_pressure
        else:
            direction, probability, level = "short", inputs.prebreak_short, inputs.short_level
            pressure = inputs.short_pressure

        # Was a setup building and has it now collapsed? Checked before
        # anything else is claimed, so an invalidation is never masked by
        # the machine immediately finding a new reason to be interested.
        if self.current.state in (PRE_BREAK_LONG, PRE_BREAK_SHORT,
                                  HIGH_PROBABILITY, A_PLUS, PRE_SIGNAL):
            if strongest < WATCH_PRESSURE * 0.5 and pressure < WATCH_PRESSURE:
                return (INVALIDATED, self.current.direction, 0.0,
                        "the setup that was building has gone", strongest, level)

        if self.current.state == INVALIDATED and \
                now - self.current.changed_at < INVALIDATION_COOLDOWN_SECONDS:
            return (INVALIDATED, self.current.direction, 0.0,
                    "cooling off after an invalidation", strongest, self.current.level)

        # ---- the gates ------------------------------------------------
        #
        # Everything below this point can name a SIDE, so everything that
        # forbids naming one has to be checked first.
        #
        # That ordering is the fix for a real defect: REVERSAL_CANDIDATE
        # sat ABOVE these checks and returns a direction, so a liquidation
        # flush produced a directional call regardless of how little of
        # the engine had answered and regardless of whether its layers
        # agreed. The confidence floor existed and did not apply to the
        # one state most likely to fire on thin, fast-moving data.
        blocked = self._blocked(inputs)
        if blocked:
            return (WATCH, direction, pressure, blocked, probability, level)

        # A flush that has exhausted, with pressure now leaning against the
        # direction it flushed in.
        if inputs.liquidation_state == "EXHAUSTION":
            if inputs.long_pressure >= REVERSAL_PRESSURE and \
                    inputs.long_pressure > inputs.short_pressure:
                return (REVERSAL_CANDIDATE, "long", inputs.long_pressure,
                        "liquidations exhausted and pressure has turned up",
                        inputs.prebreak_long, inputs.long_level)
            if inputs.short_pressure >= REVERSAL_PRESSURE and \
                    inputs.short_pressure > inputs.long_pressure:
                return (REVERSAL_CANDIDATE, "short", inputs.short_pressure,
                        "liquidations exhausted and pressure has turned down",
                        inputs.prebreak_short, inputs.short_level)

        capped_top = (HIGH_PROBABILITY if inputs.conflict_level == CONFLICT_MEDIUM
                      else A_PLUS)

        if probability >= thresholds.a_plus_probability and pressure >= PRE_SIGNAL_PRESSURE:
            return (capped_top, direction, pressure,
                    f"break score {probability:.0f} with pressure {pressure:.0f}"
                    + ("" if capped_top == A_PLUS else " (capped: layers partly disagree)"),
                    probability, level)
        if probability >= thresholds.high_probability:
            return (HIGH_PROBABILITY, direction, pressure,
                    f"break score {probability:.0f}", probability, level)
        if probability >= thresholds.prebreak_probability:
            state = PRE_BREAK_LONG if direction == "long" else PRE_BREAK_SHORT
            return (state, direction, pressure,
                    f"{direction} break building at {level}", probability, level)
        if pressure >= PRE_SIGNAL_PRESSURE:
            return (PRE_SIGNAL, direction, pressure,
                    f"pressure {pressure:.0f} without a level under stress yet",
                    probability, level)
        if max(inputs.long_pressure, inputs.short_pressure) >= WATCH_PRESSURE:
            return (WATCH, direction, pressure, "something is building", probability, level)
        return (IDLE, "", pressure, "nothing worth naming", probability, level)

    def as_dict(self) -> Dict[str, object]:
        return {
            "current": self.current.as_dict(),
            "history": [s.as_dict() for s in self.history[-20:]],
        }
