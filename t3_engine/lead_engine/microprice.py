"""Microprice, its short-horizon deltas, and the bias they imply.

The microprice is computed in orderbook_engine (it is a property of the
touch and belongs with the book). What lives here is its HISTORY: the
same number a quarter-second, a second, three and five seconds ago, and
what the differences say.

Why the differences rather than the level: the microprice sitting above
the midpoint is a snapshot of queue sizes and says very little on its
own - a single large resting bid does it. The microprice CLIMBING while
the midpoint does not is queue pressure building on one side, which is
the thing worth reading, and it is only visible as a delta.

Deltas are expressed in basis points of the midpoint so that INJUSDT at
5.8 and BTCUSDT at 60,000 produce numbers on one scale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

from t3_engine.lead_engine.rolling import TimeSeries, clamp, scale_to_unit

# The horizons the specification names.
DELTA_WINDOWS_MS = (250, 1_000, 3_000, 5_000)
DELTA_LABELS = {250: "250ms", 1_000: "1s", 3_000: "3s", 5_000: "5s"}

# Basis points of drift over five seconds that counts as a full-strength
# reading. Saturating rather than normalising - see rolling.scale_to_unit.
FULL_SCALE_BPS = 6.0

# How far the microprice must sit from the midpoint, in basis points, to
# call the book leaning rather than balanced.
BIAS_BPS = 0.5


@dataclass
class MicropriceState:
    symbol: str
    series: TimeSeries = field(default_factory=lambda: TimeSeries(horizon_ms=30_000))
    mid_series: TimeSeries = field(default_factory=lambda: TimeSeries(horizon_ms=30_000))

    def observe(self, timestamp_ms: int, microprice: Optional[float],
                midpoint: Optional[float]) -> None:
        if microprice is None or midpoint is None or midpoint <= 0:
            return
        self.series.add(timestamp_ms, float(microprice))
        self.mid_series.add(timestamp_ms, float(midpoint))

    def _delta_bps(self, window_ms: int) -> Optional[float]:
        values = self.series.window(window_ms)
        mids = self.mid_series.window(window_ms)
        if len(values) < 2 or not mids:
            return None
        reference = float(mids[-1])
        if reference <= 0:
            return None
        return (float(values[-1]) - float(values[0])) / reference * 10_000.0

    def deltas(self) -> Dict[str, Optional[float]]:
        return {f"microprice_delta_{DELTA_LABELS[w]}": self._delta_bps(w)
                for w in DELTA_WINDOWS_MS}

    def offset_bps(self) -> Optional[float]:
        """Where the microprice sits relative to the midpoint, right now."""
        micro = self.series.newest()
        mid = self.mid_series.newest()
        if micro is None or mid is None or float(mid) <= 0:
            return None
        return (float(micro) - float(mid)) / float(mid) * 10_000.0

    def bias(self) -> str:
        offset = self.offset_bps()
        if offset is None:
            return "unknown"
        if offset > BIAS_BPS:
            return "bullish"
        if offset < -BIAS_BPS:
            return "bearish"
        return "neutral"

    def pressure_component(self) -> float:
        """-1..+1 for the pressure score.

        Two thirds drift, one third level. The drift is the informative
        half (see the module docstring); the level still earns a vote
        because a book that has been leaning for a while and has stopped
        moving is not the same as a balanced one."""
        drift = self._delta_bps(5_000)
        offset = self.offset_bps()
        score = 0.0
        if drift is not None:
            score += 0.67 * scale_to_unit(drift, FULL_SCALE_BPS)
        if offset is not None:
            score += 0.33 * scale_to_unit(offset, FULL_SCALE_BPS / 2.0)
        return clamp(score)

    def as_dict(self) -> Dict[str, object]:
        out: Dict[str, object] = {"microprice": self.series.newest(),
                                  "microprice_offset_bps": self.offset_bps(),
                                  "microprice_bias": self.bias()}
        out.update(self.deltas())
        return out
