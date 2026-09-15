"""Cumulative volume delta, and what it says when it disagrees with price.

CVD is the running total of taker buy volume minus taker sell volume. On
its own it is a line that goes up when buyers are crossing; its value is
in the four-way comparison with price, which is why this module exists
separately from trade_flow.py rather than being one more field on it.

  price up,   CVD up    - buyers are paying up and the price is following.
                          Agreement. The dull, healthy case.
  price up,   CVD down  - price is rising on sellers crossing. Someone is
                          lifting it without buying it, or passive bids
                          are absorbing the selling. Divergence, bearish.
  price down, CVD down  - agreement, downward.
  price down, CVD up    - buyers are crossing and price falls anyway:
                          supply above is absorbing them. Divergence,
                          bullish for the same reason in reverse.

The two divergence cases are the informative ones and they are named
explicitly rather than collapsed into a signed number, because "price and
flow disagree" is a different statement from "flow is weak".

No lookahead: every reading compares the current window against the state
at the START of that same window, never against anything later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from t3_engine.lead_engine.rolling import TimeSeries, clamp
from t3_engine.lead_engine.trade_flow import Trade

# Windows the divergence check is evaluated over. Short enough to be
# early, long enough that one large print cannot define the reading.
DIVERGENCE_WINDOWS_MS = (15_000, 60_000, 300_000)
DIVERGENCE_LABELS = {15_000: "15s", 60_000: "60s", 300_000: "5m"}

AGREEMENT_UP = "price_up_cvd_up"
AGREEMENT_DOWN = "price_down_cvd_down"
DIVERGENCE_BEARISH = "price_up_cvd_down"
DIVERGENCE_BULLISH = "price_down_cvd_up"
FLAT = "flat"

# A move smaller than this fraction of price is not a direction.
PRICE_EPSILON = 1e-5


@dataclass
class CvdState:
    """The running total plus enough history to answer any window."""

    symbol: str
    cumulative: float = 0.0
    series: TimeSeries = field(default_factory=lambda: TimeSeries(horizon_ms=300_000))
    price_series: TimeSeries = field(default_factory=lambda: TimeSeries(horizon_ms=300_000))

    def add(self, trade: Trade) -> None:
        self.cumulative += trade.quantity if trade.is_taker_buy else -trade.quantity
        self.series.add(trade.timestamp_ms, self.cumulative)
        self.price_series.add(trade.timestamp_ms, trade.price)

    # ---- windowed readings ----

    def _change(self, series: TimeSeries, window_ms: int) -> Optional[float]:
        values = series.window(window_ms)
        if len(values) < 2:
            return None
        return float(values[-1]) - float(values[0])

    def cvd_change(self, window_ms: int) -> Optional[float]:
        return self._change(self.series, window_ms)

    def price_change(self, window_ms: int) -> Optional[float]:
        return self._change(self.price_series, window_ms)

    def relationship(self, window_ms: int) -> str:
        price = self.price_change(window_ms)
        flow = self.cvd_change(window_ms)
        if price is None or flow is None:
            return "unknown"
        reference = self.price_series.newest()
        tolerance = abs(float(reference)) * PRICE_EPSILON if reference else 0.0
        if abs(price) <= tolerance or flow == 0:
            return FLAT
        if price > 0:
            return AGREEMENT_UP if flow > 0 else DIVERGENCE_BEARISH
        return AGREEMENT_DOWN if flow < 0 else DIVERGENCE_BULLISH

    def relationships(self) -> Dict[str, str]:
        return {DIVERGENCE_LABELS[w]: self.relationship(w) for w in DIVERGENCE_WINDOWS_MS}

    def divergence(self) -> Optional[str]:
        """The first divergence found, shortest window first, or None.

        Shortest first because the point of this engine is to be early;
        a 5m divergence that the 15s window already showed is the same
        event reported late."""
        for window in DIVERGENCE_WINDOWS_MS:
            state = self.relationship(window)
            if state in (DIVERGENCE_BULLISH, DIVERGENCE_BEARISH):
                return state
        return None

    # ---- scores ----

    def pressure_component(self, window_ms: int = 60_000) -> float:
        """-1..+1 from the recent CVD slope, normalised by how much it has
        moved across the whole retained history.

        Self-scaling on purpose: an instrument whose CVD swings by
        thousands and one whose CVD swings by tens both end up on the same
        axis, without either needing a hand-set constant."""
        change = self.cvd_change(window_ms)
        if change is None:
            return 0.0
        values = [float(v) for v in self.series.all()]
        if len(values) < 3:
            return 0.0
        spread = max(values) - min(values)
        if spread <= 0:
            return 0.0
        return clamp(change / spread)

    def as_dict(self) -> Dict[str, object]:
        return {
            "cvd": round(self.cumulative, 8),
            "cvd_change": {DIVERGENCE_LABELS[w]: self.cvd_change(w)
                           for w in DIVERGENCE_WINDOWS_MS},
            "price_change": {DIVERGENCE_LABELS[w]: self.price_change(w)
                             for w in DIVERGENCE_WINDOWS_MS},
            "relationships": self.relationships(),
            "divergence": self.divergence(),
        }


def history_points(state: CvdState, points: int = 120) -> List[Tuple[int, float]]:
    """A thinned CVD line for the UI. Thinned because the tab redraws it
    several times a second and five minutes of raw prints is thousands of
    points nobody can see."""
    stamped = state.series.stamped()
    if len(stamped) <= points:
        return [(int(t), float(v)) for t, v in stamped]
    step = len(stamped) / float(points)
    return [(int(stamped[int(i * step)][0]), float(stamped[int(i * step)][1]))
            for i in range(points)]
