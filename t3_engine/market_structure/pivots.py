"""Causal swing-high/low (ZigZag) pivot detection.

A pivot is only ever confirmed once price has moved AWAY from it by at
least `deviation_pct`. That is the whole no-lookahead trick: the detector
never "knows" a candle was the high of the move until later candles prove
it by reversing away from it. `Pivot.confirmed_at_index` records exactly
which candle did the confirming, so any downstream consumer can check it
was using only data available at that time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from t3_engine.common.models import Candle, Pivot


@dataclass
class _Candidate:
    price: float
    index: int
    timestamp: int


class ZigZagPivotDetector:
    def __init__(self, deviation_pct: float = 0.5):
        """deviation_pct: minimum retracement (in % of the extreme price)
        required to confirm a swing point. Smaller = more sensitive/noisy,
        larger = fewer but more significant pivots."""
        self.deviation = deviation_pct / 100.0
        self.tracking_type: Optional[str] = None  # "HIGH" or "LOW"
        self._extreme: Optional[_Candidate] = None
        self._alt_extreme: Optional[_Candidate] = None  # only used pre-bootstrap
        self.pivots: List[Pivot] = []

    @property
    def last_pivot(self) -> Optional[Pivot]:
        return self.pivots[-1] if self.pivots else None

    def _confirm(self, kind: str, extreme: _Candidate, at_index: int) -> Pivot:
        pivot = Pivot(index=extreme.index, timestamp=extreme.timestamp, price=extreme.price,
                      kind=kind, confirmed_at_index=at_index)
        self.pivots.append(pivot)
        return pivot

    def update(self, index: int, candle: Candle) -> Optional[Pivot]:
        if not candle.closed:
            raise ValueError("ZigZagPivotDetector must only be fed CLOSED candles")

        if self.tracking_type is None:
            return self._bootstrap(index, candle)

        if self.tracking_type == "HIGH":
            if candle.high > self._extreme.price:
                self._extreme = _Candidate(candle.high, index, candle.close_time)
            if self._extreme.price > 0 and (self._extreme.price - candle.low) / self._extreme.price >= self.deviation:
                pivot = self._confirm("HIGH", self._extreme, index)
                self.tracking_type = "LOW"
                self._extreme = _Candidate(candle.low, index, candle.close_time)
                return pivot
            return None
        else:
            if candle.low < self._extreme.price:
                self._extreme = _Candidate(candle.low, index, candle.close_time)
            if self._extreme.price > 0 and (candle.high - self._extreme.price) / self._extreme.price >= self.deviation:
                pivot = self._confirm("LOW", self._extreme, index)
                self.tracking_type = "HIGH"
                self._extreme = _Candidate(candle.high, index, candle.close_time)
                return pivot
            return None

    def _bootstrap(self, index: int, candle: Candle) -> Optional[Pivot]:
        """Before the very first pivot, we don't yet know if the series
        will turn out to swing up or down first, so both a tentative high
        and a tentative low candidate are tracked until one of them
        reverses by the deviation threshold."""
        if self._extreme is None:
            self._extreme = _Candidate(candle.high, index, candle.close_time)
            self._alt_extreme = _Candidate(candle.low, index, candle.close_time)
            return None

        if candle.high > self._extreme.price:
            self._extreme = _Candidate(candle.high, index, candle.close_time)
        if candle.low < self._alt_extreme.price:
            self._alt_extreme = _Candidate(candle.low, index, candle.close_time)

        if self._extreme.price > 0 and (self._extreme.price - candle.low) / self._extreme.price >= self.deviation:
            pivot = self._confirm("HIGH", self._extreme, index)
            self.tracking_type = "LOW"
            self._extreme = _Candidate(candle.low, index, candle.close_time)
            self._alt_extreme = None
            return pivot

        if self._alt_extreme.price > 0 and (candle.high - self._alt_extreme.price) / self._alt_extreme.price >= self.deviation:
            pivot = self._confirm("LOW", self._alt_extreme, index)
            self.tracking_type = "HIGH"
            self._extreme = _Candidate(candle.high, index, candle.close_time)
            self._alt_extreme = None
            return pivot

        return None
