"""Open interest / funding / long-short ratio tracking (spec sections 2, 9).

These are polled via REST (they are not part of the WS trade/kline stream)
so this module is a simple stateful accumulator: the market_data REST
client calls `.update(...)` on a timer, and the signal engine reads the
resulting `.score()` alongside everything else. Like Fibonacci, these are
explicitly weighted at only 5% (section 23) - real but minor confirmation
signals, never a trigger on their own.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

from t3_engine.common.types import Direction


@dataclass
class OpenInterestPoint:
    timestamp: int
    open_interest: float


@dataclass
class FundingPoint:
    timestamp: int
    funding_rate: float


class DerivativesTracker:
    def __init__(self, history_len: int = 50):
        self.oi_history: Deque[OpenInterestPoint] = deque(maxlen=history_len)
        self.funding_history: Deque[FundingPoint] = deque(maxlen=history_len)
        self.long_short_ratio: Optional[float] = None

    def update_open_interest(self, timestamp: int, open_interest: float) -> None:
        self.oi_history.append(OpenInterestPoint(timestamp, open_interest))

    def update_funding(self, timestamp: int, funding_rate: float) -> None:
        self.funding_history.append(FundingPoint(timestamp, funding_rate))

    def update_long_short_ratio(self, ratio: float) -> None:
        self.long_short_ratio = ratio

    def oi_change_pct(self, lookback: int = 5) -> Optional[float]:
        if len(self.oi_history) < 2:
            return None
        window = list(self.oi_history)[-lookback:]
        if window[0].open_interest == 0:
            return None
        return (window[-1].open_interest - window[0].open_interest) / window[0].open_interest

    def score(self, direction: Direction) -> float:
        """0..1: rising OI + price moving with direction = healthy trend
        (fresh positioning, not just short-covering/long-liquidation).
        Extreme funding/long-short skew AGAINST the trade direction is
        treated as a mild tailwind (crowded opposite positioning = fuel for
        a squeeze); extreme skew WITH the trade direction is a mild
        headwind (crowded trade, more prone to a violent flush)."""
        parts = []

        oi_change = self.oi_change_pct()
        if oi_change is not None:
            parts.append(1.0 if oi_change > 0 else 0.3)

        if self.funding_history:
            funding = self.funding_history[-1].funding_rate
            crowded_long = funding > 0.0005
            crowded_short = funding < -0.0005
            if direction == Direction.UP:
                parts.append(0.3 if crowded_long else (0.8 if crowded_short else 0.5))
            else:
                parts.append(0.3 if crowded_short else (0.8 if crowded_long else 0.5))

        if self.long_short_ratio is not None:
            if direction == Direction.UP:
                parts.append(0.8 if self.long_short_ratio < 1.0 else 0.4)
            else:
                parts.append(0.8 if self.long_short_ratio > 1.0 else 0.4)

        return sum(parts) / len(parts) if parts else 0.5
