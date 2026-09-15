"""Forced closes: who is being taken out, how fast, and whether it is done.

Bybit's `allLiquidation.SYMBOL` reports the side of the LIQUIDATED
POSITION: S="Buy" is a long being taken out, S="Sell" is a short. That is
the opposite of the older `liquidation` topic, which reported the closing
ORDER's side - and reasoning from the older convention is exactly how
this came to be inverted in the first build, filing every long flush as a
short flush. The mapping therefore lives in one place -
`side_is_long_liquidation` - and is asserted by a test rather than
remembered.

The states are about shape, not size:

  LONG_FLUSH     longs being force-sold, fast enough to matter.
  SHORT_SQUEEZE  the mirror.
  CASCADE        accelerating - each second worse than the last. This is
                 the one that moves price on its own, because forced
                 sellers do not care what they get.
  EXHAUSTION     it WAS fast and is now decaying. The interesting state:
                 the supply that was forced out has been.
  NEUTRAL        nothing worth naming.

EXHAUSTION is deliberately not "liquidations stopped". It requires that
they were heavy recently and have slowed, which is a claim about a
sequence and cannot be read off a single window - hence the 60s window
being compared against the 5s one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

from t3_engine.lead_engine.config import Thresholds
from t3_engine.lead_engine.rolling import TimeSeries, clamp, scale_to_unit

WINDOWS_MS = (1_000, 5_000, 15_000, 60_000)
WINDOW_LABELS = {1_000: "1s", 5_000: "5s", 15_000: "15s", 60_000: "60s"}

LONG_FLUSH = "LONG_FLUSH"
SHORT_SQUEEZE = "SHORT_SQUEEZE"
CASCADE = "CASCADE"
EXHAUSTION = "EXHAUSTION"
NEUTRAL = "NEUTRAL"

# A side must carry this share of the liquidated notional for the state to
# be named after it; below that it is two-sided noise.
DOMINANCE = 0.65

# Exhaustion: the peak 5s rate in the last minute must have been at least
# this multiple of the CURRENT 5s rate before the flush counts as spent.
# Measured against the PEAK, never against the minute's average: a four
# second flush of 90k averaged over sixty seconds is 1.5k/s, which is
# below every sensible threshold, and comparing that average to the
# threshold made exhaustion undetectable - the state simply never fired.
EXHAUSTION_DECAY = 2.5

# How far back the peak is looked for, and at what resolution.
PEAK_LOOKBACK_MS = 60_000
PEAK_BUCKET_MS = 1_000
PEAK_WINDOW_BUCKETS = 5


def side_is_long_liquidation(side: str) -> bool:
    """True when the POSITION that was liquidated was a long.

    Bybit's `allLiquidation` reports the side of the liquidated POSITION,
    not the side of the order that closed it. `S="Buy"` is a long being
    liquidated; `S="Sell"` is a short.

    This was backwards, and it was backwards for a defensible reason -
    the older `liquidation` topic reported the closing ORDER's side,
    where the logic runs the other way round (closing a long means
    selling). Reasoning from that convention gave exactly the wrong
    answer for the newer topic, and the result was not a small error:
    every long flush was filed as a short flush and vice versa, so the
    liquidation state, the flush direction and the REVERSAL_CANDIDATE it
    can produce all pointed the wrong way. Confirmed against the live
    feed.

    It is a named function with a test for the same reason it always
    was: inverting it inverts every state in this module."""
    return str(side).strip().lower().startswith("b")


@dataclass(frozen=True)
class Liquidation:
    timestamp_ms: int
    price: float
    quantity: float
    is_long: bool

    @property
    def notional(self) -> float:
        return self.price * self.quantity


@dataclass
class LiquidationEngine:
    symbol: str
    thresholds: Thresholds = field(default_factory=Thresholds)
    events: TimeSeries = field(default_factory=lambda: TimeSeries(horizon_ms=300_000))

    def add(self, event: Liquidation) -> None:
        self.events.add(event.timestamp_ms, event)

    # ---- windows ----

    def window(self, window_ms: int) -> Dict[str, float]:
        long_notional = short_notional = 0.0
        long_count = short_count = 0
        for item in self.events.window(window_ms):
            liq: Liquidation = item             # type: ignore[assignment]
            if liq.is_long:
                long_notional += liq.notional
                long_count += 1
            else:
                short_notional += liq.notional
                short_count += 1
        seconds = max(0.001, window_ms / 1000.0)
        return {
            "long_notional": round(long_notional, 2),
            "short_notional": round(short_notional, 2),
            "long_count": long_count, "short_count": short_count,
            "count": long_count + short_count,
            "velocity": round((long_notional + short_notional) / seconds, 2),
        }

    def windows(self) -> Dict[str, Dict[str, float]]:
        return {WINDOW_LABELS[w]: self.window(w) for w in WINDOWS_MS}

    def velocity(self, window_ms: int = 5_000) -> float:
        return self.window(window_ms)["velocity"]

    def acceleration(self) -> float:
        """Current 5s rate minus the 5s rate one window earlier.

        Positive and large is a cascade: the forced selling is feeding
        itself. Computed from the same series at two offsets, so it needs
        no extra state and cannot drift out of step with the windows."""
        newest = self.events.newest_timestamp()
        if newest is None:
            return 0.0
        current = sum(i.notional for i in self.events.window(5_000, now_ms=newest))  # type: ignore[attr-defined]
        previous = sum(i.notional for i in self.events.window(5_000, now_ms=newest - 5_000))  # type: ignore[attr-defined]
        return round((current - previous) / 5.0, 2)

    def peak_velocity(self, lookback_ms: int = PEAK_LOOKBACK_MS) -> float:
        """The highest 5-second rate seen in the lookback.

        Bucketed by the second and rolled, so this is one pass over the
        retained events rather than sixty overlapping window scans."""
        newest = self.events.newest_timestamp()
        if newest is None:
            return 0.0
        buckets: Dict[int, float] = {}
        for item in self.events.window(lookback_ms, now_ms=newest):
            liq: Liquidation = item             # type: ignore[assignment]
            key = liq.timestamp_ms // PEAK_BUCKET_MS
            buckets[key] = buckets.get(key, 0.0) + liq.notional
        if not buckets:
            return 0.0
        first, last = min(buckets), max(buckets)
        peak = 0.0
        for start in range(first, last + 1):
            total = sum(buckets.get(start + offset, 0.0)
                        for offset in range(PEAK_WINDOW_BUCKETS))
            peak = max(peak, total / (PEAK_WINDOW_BUCKETS * PEAK_BUCKET_MS / 1000.0))
        return round(peak, 2)

    def _exhausted(self, current_velocity: float) -> bool:
        peak = self.peak_velocity()
        if peak < self.thresholds.liquidation_velocity:
            return False                # there was never a flush to exhaust
        return current_velocity * EXHAUSTION_DECAY <= peak

    # ---- state ----

    def state(self) -> str:
        recent = self.window(5_000)
        total = recent["long_notional"] + recent["short_notional"]
        velocity = recent["velocity"]

        if total <= 0:
            return EXHAUSTION if self._exhausted(0.0) else NEUTRAL

        if self.acceleration() >= self.thresholds.liquidation_cascade_acceleration:
            return CASCADE

        if velocity < self.thresholds.liquidation_velocity:
            return EXHAUSTION if self._exhausted(velocity) else NEUTRAL

        long_share = recent["long_notional"] / total
        if long_share >= DOMINANCE:
            return LONG_FLUSH
        if (1.0 - long_share) >= DOMINANCE:
            return SHORT_SQUEEZE
        return NEUTRAL

    def pressure_component(self) -> float:
        """-1..+1.

        Sign is the contrarian one and that is the point: longs being
        forced out is SELLING now, but it is also supply being removed,
        and the engine that reads it as merely bearish will sell the low
        of every flush. So a long flush scores NEGATIVE while it
        accelerates and flips POSITIVE once it exhausts. The state
        machine, not the sign alone, is what makes that readable."""
        state = self.state()
        recent = self.window(5_000)
        total = recent["long_notional"] + recent["short_notional"]
        magnitude = scale_to_unit(recent["velocity"],
                                  self.thresholds.liquidation_velocity * 4.0)
        if total <= 0:
            magnitude = 0.0
        if state == LONG_FLUSH:
            return clamp(-magnitude)
        if state == SHORT_SQUEEZE:
            return clamp(magnitude)
        if state == CASCADE:
            long_share = (recent["long_notional"] / total) if total else 0.5
            return clamp(-magnitude if long_share >= 0.5 else magnitude)
        if state == EXHAUSTION:
            minute = self.window(PEAK_LOOKBACK_MS)
            minute_total = minute["long_notional"] + minute["short_notional"]
            if minute_total <= 0:
                return 0.0
            long_share = minute["long_notional"] / minute_total
            # The flush is over; what it removed was the weak side.
            return clamp(0.5 if long_share >= 0.5 else -0.5)
        return 0.0

    def as_dict(self) -> Dict[str, object]:
        return {
            "windows": self.windows(),
            "velocity": self.velocity(),
            "peak_velocity_60s": self.peak_velocity(),
            "acceleration": self.acceleration(),
            "state": self.state(),
            "events_kept": len(self.events),
        }


def liquidation_from_message(item: Dict, symbol: str) -> Optional[Liquidation]:
    """One Bybit allLiquidation entry into a Liquidation, or None.

    Bybit has shipped both `v`/`p` (older single-liquidation topic) and
    `size`/`price` spellings; both are accepted rather than assuming one,
    because a rename upstream would otherwise silently zero this whole
    module."""
    try:
        stamp = int(item.get("T") or item.get("updatedTime") or 0)
        price = float(item.get("p") or item.get("price") or 0.0)
        quantity = float(item.get("v") or item.get("size") or item.get("qty") or 0.0)
    except (TypeError, ValueError):
        return None
    if price <= 0 or quantity <= 0 or stamp <= 0:
        return None
    side = item.get("S") or item.get("side") or ""
    return Liquidation(timestamp_ms=stamp, price=price, quantity=quantity,
                       is_long=side_is_long_liquidation(side))
