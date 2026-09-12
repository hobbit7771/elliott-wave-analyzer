"""Momentum indicators used as ONE input among many to the entry score
(spec section 9/23) - EMA 9/18, MACD, ADX. These are never used alone to
trigger a trade (section 1: "not just because EMA/MACD gave a signal").

Implemented as streaming/causal calculators: each `.update(value)` call
only uses the new value plus its own internal state, so replaying history
bar-by-bar in the backtester produces bit-identical output to a live run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


class EMA:
    def __init__(self, period: int):
        self.period = period
        self._alpha = 2.0 / (period + 1)
        self.value: Optional[float] = None

    def update(self, price: float) -> float:
        if self.value is None:
            self.value = price
        else:
            self.value = self._alpha * price + (1 - self._alpha) * self.value
        return self.value


class MACD:
    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        self._fast = EMA(fast)
        self._slow = EMA(slow)
        self._signal = EMA(signal)
        self.macd: Optional[float] = None
        self.signal_line: Optional[float] = None
        self.histogram: Optional[float] = None

    def update(self, price: float) -> "MACD":
        fast_v = self._fast.update(price)
        slow_v = self._slow.update(price)
        self.macd = fast_v - slow_v
        self.signal_line = self._signal.update(self.macd)
        self.histogram = self.macd - self.signal_line
        return self


@dataclass
class _Wilder:
    period: int
    value: Optional[float] = None

    def update(self, x: float) -> float:
        if self.value is None:
            self.value = x
        else:
            self.value = (self.value * (self.period - 1) + x) / self.period
        return self.value


class ADX:
    """Average Directional Index, Wilder's original smoothing."""

    def __init__(self, period: int = 14):
        self.period = period
        self._prev_high: Optional[float] = None
        self._prev_low: Optional[float] = None
        self._prev_close: Optional[float] = None
        self._tr = _Wilder(period)
        self._plus_dm = _Wilder(period)
        self._minus_dm = _Wilder(period)
        self._dx = _Wilder(period)
        self.value: Optional[float] = None
        self.plus_di: Optional[float] = None
        self.minus_di: Optional[float] = None

    def update(self, high: float, low: float, close: float) -> Optional[float]:
        if self._prev_high is None:
            self._prev_high, self._prev_low, self._prev_close = high, low, close
            return None

        up_move = high - self._prev_high
        down_move = self._prev_low - low
        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0

        tr = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))

        tr_s = self._tr.update(tr)
        plus_dm_s = self._plus_dm.update(plus_dm)
        minus_dm_s = self._minus_dm.update(minus_dm)

        self._prev_high, self._prev_low, self._prev_close = high, low, close

        if tr_s == 0:
            return self.value

        self.plus_di = 100 * plus_dm_s / tr_s
        self.minus_di = 100 * minus_dm_s / tr_s
        di_sum = self.plus_di + self.minus_di
        dx = 100 * abs(self.plus_di - self.minus_di) / di_sum if di_sum else 0.0
        self.value = self._dx.update(dx)
        return self.value


def momentum_score(macd: MACD, adx: ADX, ema9: float, ema18: float, direction_is_up: bool) -> float:
    """0..1 composite: MACD histogram direction agrees with trade
    direction, ADX confirms trend strength, EMA9/18 stack agrees."""
    score = 0.0
    parts = 0

    if macd.histogram is not None:
        parts += 1
        if (macd.histogram > 0) == direction_is_up:
            score += 1.0

    if adx.value is not None:
        parts += 1
        score += min(adx.value / 40.0, 1.0)  # ADX>=40 treated as max trend strength

    parts += 1
    if (ema9 > ema18) == direction_is_up:
        score += 1.0

    return score / parts if parts else 0.5
