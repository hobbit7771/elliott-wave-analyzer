"""Warning BEFORE the level goes, not after.

Everything else in this package measures a state. This module is the one
that makes a claim about the next few minutes, and the claim is narrow:
price is pressed against a level, and the things that normally happen
just before such a level fails are happening now.

The specification lists nine of those things for a short, and they are
implemented one function each below, each returning 0..1:

  compression        price is sitting on the level, not visiting it
  fading_bounces     each bounce off it is smaller than the last
  depth_drain        the resting depth on the defending side is shrinking
  defender_pulling   that side is being CANCELLED, not traded away
  attacker_stacking  the other side is being reinforced
  flow_pressure      aggressive flow is one-sided into the level
  microprice_lean    the touch is leaning through the level
  velocity_rising    activity is accelerating rather than dying out
  btc_alignment      BTC is already going that way
  repeated_tests     the level has been hit several times

They are combined by a weighted mean, not a rule cascade, for a specific
reason: any one of them alone is a common, meaningless event - depth
thins constantly, bounces shrink constantly - and a cascade that requires
all of them fires once a week. The weighted mean lets a strong reading on
six of ten carry, and the breakdown travels with the number so a
probability can always be taken apart.

What this is NOT: a prediction that the break will be profitable, or a
statement about how far price travels afterwards. It is "this level is
under pressure and the pressure is of the kind that precedes a break".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from t3_engine.lead_engine.config import Thresholds
from t3_engine.lead_engine.orderbook_engine import OrderBookMetrics
from t3_engine.lead_engine.rolling import TimeSeries, clamp, scale_to_unit
from t3_engine.lead_engine.smc_engine import Candle, find_swings

LONG = "long"
SHORT = "short"

# How the ten features are weighted into the probability. They sum to 1;
# the order-flow group (drain, pulling, stacking, flow) carries half,
# because those are the four that are actually early - compression and
# repeated tests describe a setup that may sit there for hours.
FEATURE_WEIGHTS: Dict[str, float] = {
    "compression": 0.10,
    "fading_bounces": 0.10,
    "depth_drain": 0.13,
    "defender_pulling": 0.15,
    "attacker_stacking": 0.10,
    "flow_pressure": 0.14,
    "microprice_lean": 0.08,
    "velocity_rising": 0.08,
    "btc_alignment": 0.07,
    "repeated_tests": 0.05,
}

# A level must have been touched at least this often before "repeated
# tests" contributes anything at all.
MIN_TESTS = 2

# Depth history is sampled at this cadence for the drain measurement.
DEPTH_SAMPLE_MS = 1_000
DEPTH_WINDOW_MS = 30_000


@dataclass
class Level:
    price: float
    kind: str                       # "support" | "resistance"
    tests: int = 0
    first_seen_ms: int = 0
    last_test_ms: int = 0
    bounces: List[float] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return {"price": round(self.price, 8), "kind": self.kind, "tests": self.tests,
                "last_test_ms": self.last_test_ms,
                "bounces": [round(b, 6) for b in self.bounces[-5:]]}


class LevelTracker:
    """Swing-derived levels, and how price has behaved at them.

    Levels come from closed-candle swings only (see smc_engine.find_swings
    for why), so a level can never be created by the bar currently
    forming - which would let the engine 'discover' support at exactly the
    price that just printed."""

    def __init__(self, thresholds: Optional[Thresholds] = None) -> None:
        self.thresholds = thresholds or Thresholds()
        self.levels: List[Level] = []

    def rebuild(self, candles: List[Candle]) -> None:
        swings = find_swings(candles)
        if not swings:
            return
        tolerance = self.thresholds.compression_pct
        fresh: List[Level] = []
        for swing in swings:
            kind = "resistance" if swing.kind == "high" else "support"
            existing = next((lv for lv in fresh
                             if lv.kind == kind and swing.price > 0
                             and abs(lv.price - swing.price) / swing.price <= tolerance), None)
            if existing is None:
                fresh.append(Level(price=swing.price, kind=kind, tests=1,
                                   first_seen_ms=swing.timestamp_ms,
                                   last_test_ms=swing.timestamp_ms))
            else:
                # Same level revisited: that is a test, and the count is
                # what "tested several times" in the specification means.
                existing.tests += 1
                existing.last_test_ms = swing.timestamp_ms
                existing.price = (existing.price + swing.price) / 2.0
        self.levels = fresh
        self._measure_bounces(candles)

    def _measure_bounces(self, candles: List[Candle]) -> None:
        """How far price travelled away from each level between touches.

        Shrinking bounces mean the level is being defended with less and
        less conviction, which is the classic pre-break tell and the
        reason this is measured rather than assumed.

        A bounce is closed by the RETURN to the level, and only a bar that
        actually left the zone can contribute to one. The first version
        recorded a bounce on every consecutive bar sitting on the level,
        each time resetting the extreme to the level itself - so every
        bounce measured zero, every one was filtered out as empty, and the
        feature silently reported 0.0 for a level being tested three
        times. Found in replay: `fading_bounces` was the only feature that
        never moved."""
        closed = [c for c in candles if c.closed]
        tolerance = self.thresholds.compression_pct
        for level in self.levels:
            level.bounces = []
            if level.price <= 0:
                continue
            away = False
            extreme = level.price
            for candle in closed:
                if level.kind == "support":
                    near = abs(candle.low - level.price) / level.price <= tolerance
                else:
                    near = abs(candle.high - level.price) / level.price <= tolerance
                if near:
                    if away:
                        # Back at the level, having genuinely left it.
                        level.bounces.append(abs(extreme - level.price) / level.price)
                        away = False
                    extreme = level.price
                    continue
                away = True
                if level.kind == "support":
                    extreme = max(extreme, candle.high)
                else:
                    extreme = min(extreme, candle.low)
            level.bounces = [b for b in level.bounces if b > 0]

    def nearest(self, price: float, kind: str) -> Optional[Level]:
        candidates = [lv for lv in self.levels if lv.kind == kind]
        if not candidates:
            return None
        if kind == "support":
            below = [lv for lv in candidates if lv.price <= price]
            return max(below, key=lambda lv: lv.price) if below else None
        above = [lv for lv in candidates if lv.price >= price]
        return min(above, key=lambda lv: lv.price) if above else None


@dataclass
class PreBreakInputs:
    """Everything the evaluation reads, gathered by the caller.

    A dataclass rather than a pile of arguments so that replay can build
    the identical input from recorded events and get the identical answer
    - which is what makes the backtest in replay.py mean anything."""

    price: float
    book: OrderBookMetrics
    microprice_offset_bps: Optional[float]
    cvd_component: float
    flow_component: float
    velocity_zscore: float
    velocity_acceleration: float
    btc_lead_score: float
    candles: List[Candle] = field(default_factory=list)


@dataclass
class PreBreakResult:
    direction: str
    level: Optional[float]
    probability: float
    features: Dict[str, float] = field(default_factory=dict)
    tests: int = 0
    note: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {
            "direction": self.direction, "level": self.level,
            "break_probability": round(self.probability, 2),
            "features": {k: round(v, 4) for k, v in self.features.items()},
            "tests": self.tests, "note": self.note,
        }


class PreBreakEngine:
    def __init__(self, symbol: str, thresholds: Optional[Thresholds] = None) -> None:
        self.symbol = symbol.upper()
        self.thresholds = thresholds or Thresholds()
        self.levels = LevelTracker(self.thresholds)
        self._bid_depth: TimeSeries = TimeSeries(horizon_ms=DEPTH_WINDOW_MS * 2)
        self._ask_depth: TimeSeries = TimeSeries(horizon_ms=DEPTH_WINDOW_MS * 2)
        self._last_depth_sample = 0

    def observe_depth(self, timestamp_ms: int, bid_depth: float, ask_depth: float) -> None:
        """Sampled, not recorded on every delta. Thirty seconds of raw
        book updates is thousands of points describing the same slope."""
        if timestamp_ms - self._last_depth_sample < DEPTH_SAMPLE_MS:
            return
        self._last_depth_sample = timestamp_ms
        self._bid_depth.add(timestamp_ms, float(bid_depth))
        self._ask_depth.add(timestamp_ms, float(ask_depth))

    # ---- the ten features, each 0..1 ----

    def _compression(self, price: float, level: Level) -> float:
        if level.price <= 0:
            return 0.0
        distance = abs(price - level.price) / level.price
        # Full marks when sitting on it, nothing once a whole tolerance
        # band away.
        return clamp(1.0 - distance / max(1e-9, self.thresholds.compression_pct), 0.0, 1.0)

    @staticmethod
    def _fading_bounces(level: Level) -> float:
        bounces = level.bounces[-4:]
        if len(bounces) < 2:
            return 0.0
        decreases = sum(1 for a, b in zip(bounces, bounces[1:]) if b < a)
        share = decreases / float(len(bounces) - 1)
        # Weighted by how much smaller the last one is than the first.
        shrink = 0.0
        if bounces[0] > 0:
            shrink = clamp(1.0 - bounces[-1] / bounces[0], 0.0, 1.0)
        return clamp(0.5 * share + 0.5 * shrink, 0.0, 1.0)

    def _depth_drain(self, direction: str) -> float:
        series = self._bid_depth if direction == SHORT else self._ask_depth
        values = [float(v) for v in series.window(DEPTH_WINDOW_MS)]
        if len(values) < 4:
            return 0.0
        first, last = values[0], values[-1]
        if first <= 0:
            return 0.0
        drop = (first - last) / first
        return clamp(scale_to_unit(drop, 0.5), 0.0, 1.0)

    def _defender_pulling(self, direction: str, book: OrderBookMetrics) -> float:
        """Cancellation on the defending side, relative to its own
        replenishment. A side that is being pulled AND refilled is being
        traded; a side that is being pulled and not refilled is leaving."""
        if direction == SHORT:
            pulling, replenishment = book.bid_pulling, book.bid_replenishment
        else:
            pulling, replenishment = book.ask_pulling, book.ask_replenishment
        total = pulling + replenishment
        if total <= 0:
            return 0.0
        return clamp(pulling / total, 0.0, 1.0)

    @staticmethod
    def _attacker_stacking(direction: str, book: OrderBookMetrics) -> float:
        if direction == SHORT:
            building, opposing = book.ask_replenishment, book.bid_replenishment
            stacked = book.stacked_ask_levels
        else:
            building, opposing = book.bid_replenishment, book.ask_replenishment
            stacked = book.stacked_bid_levels
        total = building + opposing
        share = (building / total) if total > 0 else 0.0
        return clamp(0.7 * share + 0.3 * clamp(stacked / 5.0, 0.0, 1.0), 0.0, 1.0)

    @staticmethod
    def _flow_pressure(direction: str, flow_component: float, cvd_component: float) -> float:
        sign = -1.0 if direction == SHORT else 1.0
        combined = 0.5 * flow_component + 0.5 * cvd_component
        return clamp(sign * combined, 0.0, 1.0)

    @staticmethod
    def _microprice_lean(direction: str, offset_bps: Optional[float]) -> float:
        if offset_bps is None:
            return 0.0
        sign = -1.0 if direction == SHORT else 1.0
        return clamp(scale_to_unit(sign * offset_bps, 3.0), 0.0, 1.0)

    def _velocity_rising(self, zscore: float, acceleration: float) -> float:
        magnitude = clamp(scale_to_unit(max(0.0, zscore),
                                        self.thresholds.velocity_zscore_extreme), 0.0, 1.0)
        rising = 1.0 if acceleration > 0 else 0.0
        return clamp(0.7 * magnitude + 0.3 * rising, 0.0, 1.0)

    @staticmethod
    def _btc_alignment(direction: str, lead_score: float) -> float:
        sign = -1.0 if direction == SHORT else 1.0
        return clamp(sign * lead_score, 0.0, 1.0)

    @staticmethod
    def _repeated_tests(level: Level) -> float:
        if level.tests < MIN_TESTS:
            return 0.0
        return clamp((level.tests - MIN_TESTS + 1) / 4.0, 0.0, 1.0)

    # ---- evaluation ----

    def evaluate(self, direction: str, inputs: PreBreakInputs) -> PreBreakResult:
        if inputs.candles:
            self.levels.rebuild(inputs.candles)
        kind = "support" if direction == SHORT else "resistance"
        level = self.levels.nearest(inputs.price, kind)
        if level is None:
            return PreBreakResult(direction=direction, level=None, probability=0.0,
                                  note=f"no {kind} identified below the visible swings")
        if not inputs.book.synced:
            return PreBreakResult(direction=direction, level=level.price, probability=0.0,
                                  tests=level.tests,
                                  note="order book not synced; no probability offered")

        features = {
            "compression": self._compression(inputs.price, level),
            "fading_bounces": self._fading_bounces(level),
            "depth_drain": self._depth_drain(direction),
            "defender_pulling": self._defender_pulling(direction, inputs.book),
            "attacker_stacking": self._attacker_stacking(direction, inputs.book),
            "flow_pressure": self._flow_pressure(direction, inputs.flow_component,
                                                 inputs.cvd_component),
            "microprice_lean": self._microprice_lean(direction, inputs.microprice_offset_bps),
            "velocity_rising": self._velocity_rising(inputs.velocity_zscore,
                                                     inputs.velocity_acceleration),
            "btc_alignment": self._btc_alignment(direction, inputs.btc_lead_score),
            "repeated_tests": self._repeated_tests(level),
        }
        probability = 100.0 * sum(FEATURE_WEIGHTS[name] * value
                                  for name, value in features.items())
        # Compression gates the whole thing. A perfect order-flow reading
        # taken while price is nowhere near the level is not a pre-break
        # warning about that level; it is a description of the market.
        probability *= (0.35 + 0.65 * features["compression"])
        return PreBreakResult(direction=direction, level=level.price,
                              probability=clamp(probability, 0.0, 100.0),
                              features=features, tests=level.tests)

    def evaluate_both(self, inputs: PreBreakInputs) -> Tuple[PreBreakResult, PreBreakResult]:
        return self.evaluate(LONG, inputs), self.evaluate(SHORT, inputs)
