"""Shared enums used across the whole T3 engine.

Keeping these in one module avoids the classic bug where two modules
define their own slightly-different `Direction` or `WaveStatus` enum and
comparisons silently fail.
"""

from __future__ import annotations

from enum import Enum


class Timeframe(str, Enum):
    """Every timeframe the engine understands, ordered fastest -> slowest."""

    S1 = "1s"
    S5 = "5s"
    S15 = "15s"
    S30 = "30s"
    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"

    @property
    def seconds(self) -> int:
        return _TF_SECONDS[self]

    def __lt__(self, other: "Timeframe") -> bool:
        if not isinstance(other, Timeframe):
            return NotImplemented
        return self.seconds < other.seconds


_TF_SECONDS = {
    Timeframe.S1: 1,
    Timeframe.S5: 5,
    Timeframe.S15: 15,
    Timeframe.S30: 30,
    Timeframe.M1: 60,
    Timeframe.M3: 180,
    Timeframe.M5: 300,
    Timeframe.M15: 900,
    Timeframe.H1: 3600,
    Timeframe.H4: 14400,
}

# Timeframes that are ALLOWED to originate a tradeable Elliott wave signal.
# Originally just (M5, M15) per the spec's section 21 (sub-minute
# confirmation drives entries on those two only, 1h/4h was context-only).
# Widened to include H1/H4 on the project owner's explicit instruction, to
# evaluate the same rule-based strategy on higher timeframes too - the
# underlying signal/risk/execution logic is unchanged, only which degrees
# are allowed to reach it. 1s-3m stays confirmation-only: those aren't
# separate tradeable degrees, they're the sub-bar detail the 5m/15m/1h/4h
# entries are supposed to (but, per the backtester's documented single-TF
# simplification, don't yet) confirm against.
TRADEABLE_TIMEFRAMES = (Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.H4)
CONFIRMATION_TIMEFRAMES = (Timeframe.S1, Timeframe.S5, Timeframe.S15, Timeframe.S30, Timeframe.M1, Timeframe.M3)
CONTEXT_TIMEFRAMES: tuple = ()


class Direction(str, Enum):
    UP = "UP"
    DOWN = "DOWN"

    def opposite(self) -> "Direction":
        return Direction.DOWN if self is Direction.UP else Direction.UP


class WaveLabel(str, Enum):
    """Elliott wave labels. Motive impulse (1..5), corrective (A,B,C) and
    diagonal/micro subwave labels (i..v) share the same status/lifecycle
    machinery, so they live in one enum distinguished by `degree`."""

    W1 = "1"
    W2 = "2"
    W3 = "3"
    W4 = "4"
    W5 = "5"
    A = "A"
    B = "B"
    C = "C"
    # D and E exist only inside a triangle (A-B-C-D-E), the one
    # corrective structure with five legs. Nothing else produces them.
    D = "D"
    E = "E"
    W = "W"
    X = "X"
    Y = "Y"
    Z = "Z"
    I = "i"
    II = "ii"
    III = "iii"
    IV = "iv"
    V = "v"

    @property
    def is_motive(self) -> bool:
        return self in (WaveLabel.W1, WaveLabel.W2, WaveLabel.W3, WaveLabel.W4, WaveLabel.W5,
                        WaveLabel.I, WaveLabel.II, WaveLabel.III, WaveLabel.IV, WaveLabel.V)

    @property
    def is_odd(self) -> bool:
        """Waves 1/3/5 (and A/C) move WITH the larger trend."""
        return self in (WaveLabel.W1, WaveLabel.W3, WaveLabel.W5, WaveLabel.I, WaveLabel.III, WaveLabel.V,
                        WaveLabel.A, WaveLabel.C)


class WaveStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    DEVELOPING = "DEVELOPING"
    PROBABLE = "PROBABLE"
    CONFIRMED = "CONFIRMED"
    INVALIDATED = "INVALIDATED"
    COMPLETED = "COMPLETED"


class StructureType(str, Enum):
    """The Elliott pattern family a completed leg is classified as."""

    IMPULSE = "IMPULSE"
    DIAGONAL_LEADING = "DIAGONAL_LEADING"
    DIAGONAL_ENDING = "DIAGONAL_ENDING"
    ZIGZAG = "ZIGZAG"
    FLAT = "FLAT"
    TRIANGLE = "TRIANGLE"
    COMBINATION = "COMBINATION"
    UNKNOWN_CORRECTION = "UNKNOWN_CORRECTION"
    UNKNOWN_IMPULSE = "UNKNOWN_IMPULSE"


class EntryStage(str, Enum):
    """Section 21 entry-timing state machine."""

    NONE = "NONE"
    PREDICTION = "PREDICTION"
    SETUP = "SETUP"
    ARMED = "ARMED"
    TRIGGERED = "TRIGGERED"
    CONFIRMED = "CONFIRMED"


class TradeSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


# Which wave labels are tradeable, and on which side, per section 3/9-13.
TRADEABLE_WAVES = {
    WaveLabel.W3: TradeSide.LONG,
    WaveLabel.W4: TradeSide.SHORT,
    WaveLabel.W5: TradeSide.LONG,
    WaveLabel.C: TradeSide.SHORT,
    WaveLabel.A: TradeSide.NONE,
    WaveLabel.B: TradeSide.NONE,
}


class SignalDecision(str, Enum):
    SIGNAL_ACCEPTED = "SIGNAL_ACCEPTED"
    SIGNAL_REJECTED = "SIGNAL_REJECTED"


class TradingMode(str, Enum):
    PAPER = "PAPER"
    TESTNET = "TESTNET"
    LIVE = "LIVE"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
