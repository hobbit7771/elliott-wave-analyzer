"""Market structure: BOS / CHoCH / liquidity sweep detection.

Built on top of confirmed ZigZag pivots (pivots.py) so it inherits the same
no-lookahead guarantee: a BOS/CHoCH event can only fire on the candle that
actually closed beyond the reference pivot level.

Definitions used here (standard price-action / smart-money terminology):
  BOS   (Break of Structure)   - price breaks a swing point IN the direction
                                  of the prevailing trend -> trend continues.
  CHoCH (Change of Character)  - price breaks a swing point AGAINST the
                                  prevailing trend -> first warning the trend
                                  may be reversing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from t3_engine.common.models import Candle, Pivot
from t3_engine.common.types import Direction


@dataclass
class StructureEvent:
    kind: str  # "BOS" or "CHoCH"
    direction: Direction
    reference_pivot: Pivot
    breaking_index: int
    breaking_timestamp: int
    breaking_price: float


@dataclass
class LiquiditySweep:
    swept_pivot: Pivot
    index: int
    timestamp: int
    wick_price: float
    close_price: float
    direction: Direction  # direction of the sweep itself (UP sweep = swept a high)


class MarketStructureTracker:
    """Feed it CONFIRMED pivots (oldest -> newest) plus the candle stream
    for liquidity-sweep detection. Maintains the last swing high/low and
    the current inferred trend (UP/DOWN/None)."""

    def __init__(self, min_break_pct: float = 0.05):
        """min_break_pct: minimum % the breaking price must clear the prior
        swing by before a BOS/CHoCH is actually declared. Without this, ANY
        new local high/low - even one a fraction of a point past the prior
        swing - fires a structure event unconditionally, which is far more
        BOS/CHoCH noise than the move actually warrants (this compounds
        with pivots.py's own deviation filter rather than replacing it: a
        pivot already required a real reversal to confirm, but the BREAK
        past the next swing on top of that had no floor of its own)."""
        self.pivots: List[Pivot] = []
        self.trend: Optional[Direction] = None
        self.last_swing_high: Optional[Pivot] = None
        self.last_swing_low: Optional[Pivot] = None
        self._prev_swing_high: Optional[Pivot] = None
        self._prev_swing_low: Optional[Pivot] = None
        self.events: List[StructureEvent] = []
        self.min_break_pct = min_break_pct / 100.0

    def _is_significant_break(self, breaking_price: float, reference_price: float) -> bool:
        if reference_price == 0:
            return True
        return abs(breaking_price - reference_price) / abs(reference_price) >= self.min_break_pct

    def on_pivot(self, pivot: Pivot) -> Optional[StructureEvent]:
        event = None
        if pivot.kind == "HIGH":
            if self.last_swing_high is not None:
                if pivot.price > self.last_swing_high.price and self._is_significant_break(
                        pivot.price, self.last_swing_high.price):
                    # higher high
                    if self.trend in (None, Direction.UP):
                        event = StructureEvent("BOS", Direction.UP, self.last_swing_high,
                                                pivot.confirmed_at_index, pivot.timestamp, pivot.price)
                        self.trend = Direction.UP
                    else:
                        # was in a downtrend, broke the last lower-high -> reversal signal
                        event = StructureEvent("CHoCH", Direction.UP, self.last_swing_high,
                                                pivot.confirmed_at_index, pivot.timestamp, pivot.price)
                        self.trend = Direction.UP
            self._prev_swing_high = self.last_swing_high
            self.last_swing_high = pivot
        else:
            if self.last_swing_low is not None:
                if pivot.price < self.last_swing_low.price and self._is_significant_break(
                        pivot.price, self.last_swing_low.price):
                    if self.trend in (None, Direction.DOWN):
                        event = StructureEvent("BOS", Direction.DOWN, self.last_swing_low,
                                                pivot.confirmed_at_index, pivot.timestamp, pivot.price)
                        self.trend = Direction.DOWN
                    else:
                        event = StructureEvent("CHoCH", Direction.DOWN, self.last_swing_low,
                                                pivot.confirmed_at_index, pivot.timestamp, pivot.price)
                        self.trend = Direction.DOWN
            self._prev_swing_low = self.last_swing_low
            self.last_swing_low = pivot

        if event is not None:
            self.events.append(event)
        return event

    def check_liquidity_sweep(self, index: int, candle: Candle) -> Optional[LiquiditySweep]:
        """A sweep: the candle's wick pierces a prior swing extreme but the
        candle CLOSES back on the other side of it (stop hunt + reclaim).
        Only considers the most recent swing high/low so it stays cheap to
        run on every closed candle."""
        if self.last_swing_high is not None and candle.high > self.last_swing_high.price and candle.close < self.last_swing_high.price:
            return LiquiditySweep(self.last_swing_high, index, candle.close_time, candle.high, candle.close, Direction.UP)
        if self.last_swing_low is not None and candle.low < self.last_swing_low.price and candle.close > self.last_swing_low.price:
            return LiquiditySweep(self.last_swing_low, index, candle.close_time, candle.low, candle.close, Direction.DOWN)
        return None

    def has_higher_high_higher_low(self) -> bool:
        return (self.last_swing_high is not None and self._prev_swing_high is not None and
                self.last_swing_high.price > self._prev_swing_high.price and
                self.last_swing_low is not None and self._prev_swing_low is not None and
                self.last_swing_low.price > self._prev_swing_low.price)

    def has_lower_high_lower_low(self) -> bool:
        return (self.last_swing_high is not None and self._prev_swing_high is not None and
                self.last_swing_high.price < self._prev_swing_high.price and
                self.last_swing_low is not None and self._prev_swing_low is not None and
                self.last_swing_low.price < self._prev_swing_low.price)
