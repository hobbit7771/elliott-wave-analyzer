"""A wave CONTEXT for the Lead Engine. Not a wave count, and not the
project's wave counter.

The analyser in this project counts Elliott waves properly: rules engine,
competing scenarios, validation, storage. This module does none of that
and must not: the specification is explicit that the existing analyser is
not to be modified, extended or driven from here, and that the Lead
Engine gets either a read-only adapter or its own simplified state.

It has both, in that order of preference:

  - If a reader is injected (`ElliottContext(reader=...)`), it is asked
    for the existing analyser's saved count and the answer is used as
    CONTEXT ONLY. The reader is a plain callable returning a dict; this
    module never imports the analyser, never writes to it, and treats a
    failure as "no context" rather than an error. That is the whole of
    the permitted coupling.
  - Otherwise it runs the deterministic state machine below over its own
    swings.

The state machine is small and its guards are the three hard rules that
can be checked without a full count: wave 2 never retraces past the start
of wave 1, wave 4 never overlaps wave 1, wave 3 is never the shortest of
1/3/5. When a guard fails the count does not get "rescued" - it resets to
a new anchor and starts again at wave 1, which is the honest response to
"what I was counting cannot be what this is".

Its output is one label and a direction, and it carries 6% of the
pressure score. That weight is the right size for a heuristic: enough to
break a tie, never enough to make a signal on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from t3_engine.lead_engine.rolling import clamp
from t3_engine.lead_engine.smc_engine import Candle, Swing, find_swings

MOTIVE_LABELS = ("1", "2", "3", "4", "5")
CORRECTIVE_LABELS = ("A", "B", "C")
SEQUENCE = MOTIVE_LABELS + CORRECTIVE_LABELS

IMPULSIVE = "impulsive"
CORRECTIVE = "corrective"
UNKNOWN = "unknown"

# Legs shorter than this share of the anchor leg are treated as noise
# rather than as the next wave.
MIN_LEG_RATIO = 0.15


@dataclass
class Leg:
    start_price: float
    end_price: float
    start_ms: int
    end_ms: int

    @property
    def length(self) -> float:
        return abs(self.end_price - self.start_price)

    @property
    def up(self) -> bool:
        return self.end_price >= self.start_price


@dataclass
class WaveContext:
    symbol: str
    label: str = ""
    phase: str = UNKNOWN
    direction: str = "flat"
    legs_counted: int = 0
    resets: int = 0
    source: str = "internal"
    note: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {
            "current_wave_candidate": self.label or None,
            "phase": self.phase,
            "direction": self.direction,
            "legs_counted": self.legs_counted,
            "resets": self.resets,
            "source": self.source,
            "note": self.note,
        }


def legs_from_swings(swings: List[Swing]) -> List[Leg]:
    return [Leg(a.price, b.price, a.timestamp_ms, b.timestamp_ms)
            for a, b in zip(swings, swings[1:])]


def _violates_hard_rules(legs: List[Leg]) -> Optional[str]:
    """The three checks that can be made from leg geometry alone.

    Returns the rule that broke, or None. These are the same rules the
    project's analyser enforces, re-stated here over this module's own
    data rather than imported - the boundary is worth more than the six
    lines of duplication, and a change to the analyser's internals must
    not be able to alter what this engine reports."""
    if len(legs) >= 2:
        one, two = legs[0], legs[1]
        if one.up and two.end_price <= one.start_price:
            return "WAVE2_PASSES_START_OF_WAVE1"
        if not one.up and two.end_price >= one.start_price:
            return "WAVE2_PASSES_START_OF_WAVE1"
    if len(legs) >= 4:
        one, four = legs[0], legs[3]
        if one.up and four.end_price <= one.end_price:
            return "WAVE4_OVERLAPS_WAVE1"
        if not one.up and four.end_price >= one.end_price:
            return "WAVE4_OVERLAPS_WAVE1"
    if len(legs) >= 5:
        one, three, five = legs[0].length, legs[2].length, legs[4].length
        if three < one and three < five:
            return "WAVE3_IS_SHORTEST"
    return None


@dataclass
class ElliottContext:
    """One symbol's wave context at one interval."""

    symbol: str
    interval: str = "5"
    reader: Optional[Callable[[str], Optional[Dict]]] = None
    candles: List[Candle] = field(default_factory=list)
    resets: int = 0

    def update(self, candle: Candle) -> None:
        if self.candles and self.candles[-1].start_ms == candle.start_ms:
            self.candles[-1] = candle
        else:
            self.candles.append(candle)
        if len(self.candles) > 400:
            self.candles = self.candles[-400:]

    # ---- the permitted read of the older system ----

    def _external(self) -> Optional[WaveContext]:
        if self.reader is None:
            return None
        try:
            payload = self.reader(self.symbol)
        except Exception:                       # noqa: BLE001 - a failing
            return None                         # reader is "no context"
        if not isinstance(payload, dict):
            return None
        label = payload.get("current_wave_candidate") or payload.get("next_label")
        if not label:
            return None
        direction = str(payload.get("direction") or "flat")
        phase = CORRECTIVE if str(label).upper() in CORRECTIVE_LABELS else IMPULSIVE
        return WaveContext(symbol=self.symbol, label=str(label), phase=phase,
                           direction=direction, source="external_read_only",
                           note="read from the project's own count; never written back")

    # ---- the self-contained machine ----

    def context(self) -> WaveContext:
        external = self._external()
        if external is not None:
            return external

        swings = find_swings(self.candles)
        if len(swings) < 3:
            return WaveContext(symbol=self.symbol, note="not enough swings yet")

        legs = legs_from_swings(swings)
        # Anchor on the most recent run of legs that does not break a hard
        # rule, walking the anchor forward until the remainder is legal.
        anchor = 0
        broken: Optional[str] = None
        while anchor < len(legs) - 1:
            candidate = legs[anchor:anchor + len(SEQUENCE)]
            broken = _violates_hard_rules(candidate)
            if broken is None:
                break
            anchor += 1
            self.resets += 1
        counted = legs[anchor:anchor + len(SEQUENCE)]
        if not counted:
            return WaveContext(symbol=self.symbol, resets=self.resets,
                               note="no legal anchor in the visible swings")

        # Drop a trailing leg that is barely a wiggle: counting it would
        # advance the label on noise.
        reference = counted[0].length
        while len(counted) > 1 and reference > 0 and counted[-1].length < MIN_LEG_RATIO * reference:
            counted = counted[:-1]

        index = min(len(counted), len(SEQUENCE)) - 1
        label = SEQUENCE[index]
        phase = CORRECTIVE if label in CORRECTIVE_LABELS else IMPULSIVE
        direction = "up" if counted[-1].up else "down"
        note = f"internal count anchored {anchor} leg(s) back"
        if broken:
            note += f"; earlier anchor rejected by {broken}"
        return WaveContext(symbol=self.symbol, label=label, phase=phase,
                           direction=direction, legs_counted=len(counted),
                           resets=self.resets, source="internal", note=note)

    def pressure_component(self) -> float:
        """-1..+1.

        Third and fifth waves of an advance lean bullish, the same waves
        of a decline lean bearish, corrections lean against the leg they
        are correcting, and everything else is zero. Modest numbers on
        purpose: this is a heuristic carrying 6% of the score, and a
        heuristic that returns +1.0 is pretending to be a measurement."""
        ctx = self.context()
        if not ctx.label:
            return 0.0
        sign = 1.0 if ctx.direction == "up" else -1.0
        if ctx.label in ("3", "5"):
            return clamp(0.6 * sign)
        if ctx.label == "1":
            return clamp(0.3 * sign)
        if ctx.label in ("2", "4"):
            return clamp(-0.2 * sign)       # a pullback inside the trend
        if ctx.label in ("A", "C"):
            return clamp(0.5 * sign)
        if ctx.label == "B":
            return clamp(-0.2 * sign)
        return 0.0

    def as_dict(self) -> Dict[str, object]:
        return self.context().as_dict()
