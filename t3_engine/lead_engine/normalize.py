"""Putting every feature on a scale that fits the instrument it came from.

The first build scored features against hardcoded full-scale constants:
microprice drift saturated at 6 basis points, liquidations at four times a
fixed 25,000/second, open interest at 2%. Six basis points of drift is
enormous on BTCUSDT and noise on DOGEUSDT, so the same constant made one
instrument permanently saturated and the other permanently asleep. That is
the defect this module exists to remove.

Everything here is ROLLING and per (symbol, feature): a value is compared
against what that feature has recently done on that instrument, never
against a number typed into the source.

Causality, because it is easy to get wrong: `update()` appends the new
value and then scores it against a history that now includes it. The
current sample is part of its own reference set - that is the past and the
present, never the future. No method here can see a later sample, and a
test asserts that appending future values does not change an earlier
answer.

Three methods, chosen per feature by what the feature is:

  zscore      - for roughly symmetric quantities that can go either way
                (CVD slope, microprice drift, OI change). Clipped at ±3
                sigma and divided by 3, so the output is -1..+1 and one
                freak print cannot rescale the axis.
  percentile  - for one-sided magnitudes where "how unusual" matters more
                than "how many sigma" (absorption, liquidation velocity,
                depth). Output 0..1, robust to fat tails by construction.
  ratio       - for quantities best read against their own typical size
                (trade size, wall size). Output is the ratio itself,
                squashed to 0..1 by `saturate` at the caller's chosen
                multiple.

A feature with too little history returns NEUTRAL and says so. A
normaliser that starts guessing from three samples is how an engine fires
on its third trade.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

# Below this many samples nothing is claimed. Eight is the same floor
# rolling.zscore already uses, kept consistent on purpose.
MIN_SAMPLES = 8

# How much history each feature keeps. 600 samples at the default
# recompute interval is roughly the last two and a half minutes of
# four-per-second frames, or ten minutes of one-per-second ones.
DEFAULT_WINDOW = 600

# Z-scores are clipped here before being divided by it, so the output is
# exactly -1..+1 and a single outlier saturates rather than dominating.
ZSCORE_CLIP = 3.0

NEUTRAL = 0.0


def clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def saturate(value: float, full_scale: float) -> float:
    """Map a magnitude onto 0..1, saturating at `full_scale`.

    Still used, but only where the full scale is itself derived from the
    instrument's own history (a multiple of its median, say) rather than
    typed in."""
    if full_scale <= 0:
        return 0.0
    return clamp(value / full_scale, 0.0, 1.0)


@dataclass
class RollingFeature:
    """One feature's recent history on one instrument."""

    name: str
    window: int = DEFAULT_WINDOW
    samples: Deque[float] = field(default_factory=deque)

    def observe(self, value: float) -> None:
        if value is None:
            return
        try:
            number = float(value)
        except (TypeError, ValueError):
            return
        if not math.isfinite(number):
            return
        self.samples.append(number)
        while len(self.samples) > self.window:
            self.samples.popleft()

    @property
    def ready(self) -> bool:
        return len(self.samples) >= MIN_SAMPLES

    def mean(self) -> float:
        return sum(self.samples) / len(self.samples) if self.samples else 0.0

    def stdev(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        average = self.mean()
        variance = sum((s - average) ** 2 for s in self.samples) / (len(self.samples) - 1)
        return math.sqrt(max(0.0, variance))

    def median(self) -> float:
        if not self.samples:
            return 0.0
        ordered = sorted(self.samples)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / 2.0

    # ---- the three methods ----

    def zscore(self, value: float) -> float:
        """-1..+1. Zero when there is not enough history to say."""
        if not self.ready:
            return NEUTRAL
        spread = self.stdev()
        if spread <= 0:
            return NEUTRAL
        raw = (float(value) - self.mean()) / spread
        return clamp(raw / ZSCORE_CLIP)

    def percentile(self, value: float) -> float:
        """0..1: the share of history at or below `value`.

        Robust to fat tails by construction, which is why the one-sided
        magnitudes use it - a single 40-sigma liquidation print moves a
        z-score off the scale and moves a percentile by one sample."""
        if not self.ready:
            return 0.5
        below = sum(1 for sample in self.samples if sample <= value)
        return below / float(len(self.samples))

    def ratio(self, value: float) -> float:
        """`value` against its own median. 1.0 means typical."""
        if not self.ready:
            return 1.0
        reference = self.median()
        if reference == 0:
            return 1.0
        return float(value) / reference


@dataclass
class Normalizer:
    """Every feature for one symbol, created on first use."""

    symbol: str
    window: int = DEFAULT_WINDOW
    features: Dict[str, RollingFeature] = field(default_factory=dict)

    def feature(self, name: str) -> RollingFeature:
        existing = self.features.get(name)
        if existing is None:
            existing = RollingFeature(name=name, window=self.window)
            self.features[name] = existing
        return existing

    def update(self, name: str, value: Optional[float], method: str = "zscore",
               full_scale_multiple: float = 3.0) -> float:
        """Record a raw value and return its normalised form.

        The one call every feature module should use. `None` in means
        neutral out and nothing recorded, so a stream that has not started
        does not poison the history with zeros."""
        if value is None:
            return NEUTRAL if method != "percentile" else 0.5
        try:
            number = float(value)
        except (TypeError, ValueError):
            return NEUTRAL
        if not math.isfinite(number):
            return NEUTRAL
        rolling = self.feature(name)
        rolling.observe(number)
        if method == "percentile":
            return rolling.percentile(number)
        if method == "ratio":
            return saturate(rolling.ratio(number), full_scale_multiple)
        return rolling.zscore(number)

    def ready(self, name: str) -> bool:
        return self.feature(name).ready

    def snapshot(self) -> Dict[str, Dict[str, float]]:
        """What each feature's reference distribution currently looks like.
        Exposed so a normalised number can be explained rather than
        trusted."""
        return {
            name: {"samples": len(rolling.samples), "median": round(rolling.median(), 8),
                   "mean": round(rolling.mean(), 8), "stdev": round(rolling.stdev(), 8),
                   "ready": rolling.ready}
            for name, rolling in sorted(self.features.items())
        }


def normalized_delta(buy_volume: float, sell_volume: float,
                     epsilon: float = 1e-9) -> float:
    """The taker imbalance, on -1..+1.

        (buy − sell) / (buy + sell + ε)

    This replaces `buy / sell` as the flow input. The ratio form is
    unbounded by construction - 0.9 bought against nothing sold is
    900,000,000 - and a feature that can be a billion cannot be summed
    with one that lives in -1..+1. The ratio is kept as a diagnostic
    field and takes no part in any score.

    +1 is aggressive buying only, −1 aggressive selling only, 0 balance.
    An empty window is 0, not undefined: nothing traded is not an
    imbalance."""
    total = float(buy_volume) + float(sell_volume)
    if total <= 0:
        return 0.0
    return clamp((float(buy_volume) - float(sell_volume)) / (total + epsilon))


def deadband(value: float, previous: Optional[float], threshold: float) -> float:
    """Hold the previous value while the change is below `threshold`.

    Used by the UI to stop colours strobing on noise, and available here
    so the same rule can be applied server-side to anything that should
    not flap."""
    if previous is None:
        return value
    return previous if abs(value - previous) < threshold else value


def agreement(values: List[float]) -> float:
    """How much a set of signed scores points the same way, 0..1.

    1.0 is unanimity, 0.0 is an even split. Weighted by magnitude, so two
    strong opposing readings disagree more than two weak ones."""
    live = [v for v in values if v is not None and abs(v) > 1e-9]
    if not live:
        return 1.0
    total = sum(abs(v) for v in live)
    if total <= 0:
        return 1.0
    net = abs(sum(live))
    return clamp(net / total, 0.0, 1.0)


def split_direction(score: float) -> Tuple[float, float]:
    """One signed score into (long, short), each 0..1.

    Separately rather than as complements, for the same reason the
    pressure score is: a featureless reading has to come out low on both,
    not 0.5 on each."""
    value = clamp(score)
    return (max(0.0, value), max(0.0, -value))
