"""Fibonacci retracement for the Lead Engine's chart.

Its own, not the analyser's `t3_engine/fibonacci/calculator.py`: that one
belongs to the wave engine, returns its `FibLevel` type and would put an
import across the isolation boundary. This is a drawing tool - two points
in, priced levels out - and it is thirty lines.

The direction rule is the part worth stating, because getting it backwards
is the classic bug and it is silent:

    A retracement is always measured FROM the end of the move BACK toward
    its start. So ratio 0 sits at the SECOND point (where the move ended)
    and ratio 1.0 at the FIRST (where it began), whichever way round the
    two points are in price.

Drawn low → high, 0.618 therefore lands 61.8% of the way back DOWN from
the high. Drawn high → low, the same ratio lands 61.8% of the way back UP.
The arithmetic is identical - `price(r) = end + (start - end) * r` - which
is exactly why it must not be "corrected" with a conditional.

Extensions past 1.0 continue beyond the start of the move, which is what
makes 1.272 and 1.618 targets rather than retracements.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# The brief's defaults, and its extensions.
RETRACEMENTS = (0.0, 0.236, 0.382, 0.5, 0.618, 0.705, 0.786, 1.0)
EXTENSIONS = (1.272, 1.414, 1.618, 2.0, 2.618)
ALL_LEVELS = RETRACEMENTS + EXTENSIONS

# Per (symbol, timeframe) - drawings on the 5m chart must not appear on
# the 1h one. Bounded so a long session cannot grow without limit.
MAX_DRAWINGS_PER_CHART = 20


def levels(start_price: float, end_price: float,
           ratios=ALL_LEVELS) -> List[Dict[str, float]]:
    """Priced levels for a move from `start_price` to `end_price`.

    Ratio 0 is AT `end_price`, ratio 1.0 is at `start_price`. See the
    module docstring for why there is no direction conditional here."""
    start = float(start_price)
    end = float(end_price)
    span = start - end
    out = []
    for ratio in ratios:
        out.append({
            "ratio": round(float(ratio), 6),
            "price": end + span * float(ratio),
            "extension": bool(ratio > 1.0),
        })
    return out


@dataclass
class Drawing:
    """One Fibonacci retracement on one chart."""

    drawing_id: str
    symbol: str
    timeframe: str
    start_time: int
    start_price: float
    end_time: int
    end_price: float
    ratios: List[float] = field(default_factory=lambda: list(ALL_LEVELS))
    hidden: bool = False
    created_at: float = field(default_factory=time.time)

    @property
    def direction(self) -> str:
        """`low_to_high` when the move drawn was up, else `high_to_low`.

        Reported rather than used: the arithmetic in `levels` is the same
        either way, and this exists so a client can label the drawing
        without re-deriving it."""
        return "low_to_high" if self.end_price >= self.start_price else "high_to_low"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.drawing_id, "symbol": self.symbol, "timeframe": self.timeframe,
            "start": {"time": self.start_time, "price": self.start_price},
            "end": {"time": self.end_time, "price": self.end_price},
            "direction": self.direction,
            "ratios": list(self.ratios),
            "hidden": self.hidden,
            "created_at": self.created_at,
            "levels": [
                {**level, "price": round(level["price"], 10)}
                for level in levels(self.start_price, self.end_price, self.ratios)
            ],
        }


class DrawingStore:
    """Drawings, keyed by (symbol, timeframe).

    In-process and per-server: the browser also keeps its own copy in
    localStorage so a drawing survives a reload even if the process
    restarts. Both are offered because they fail differently - the server
    copy survives a new device, the browser copy survives a redeploy."""

    def __init__(self) -> None:
        self._charts: Dict[str, List[Drawing]] = {}
        self._sequence = 0

    @staticmethod
    def key(symbol: str, timeframe: str) -> str:
        return f"{str(symbol).upper()}:{str(timeframe).lower()}"

    def list(self, symbol: str, timeframe: str) -> List[Drawing]:
        return list(self._charts.get(self.key(symbol, timeframe), ()))

    def add(self, symbol: str, timeframe: str, start_time: int, start_price: float,
            end_time: int, end_price: float,
            ratios: Optional[List[float]] = None) -> Drawing:
        self._sequence += 1
        drawing = Drawing(
            drawing_id=f"fib-{self._sequence}", symbol=str(symbol).upper(),
            timeframe=str(timeframe).lower(), start_time=int(start_time),
            start_price=float(start_price), end_time=int(end_time),
            end_price=float(end_price),
            ratios=[float(r) for r in (ratios or ALL_LEVELS)],
        )
        bucket = self._charts.setdefault(self.key(symbol, timeframe), [])
        bucket.append(drawing)
        while len(bucket) > MAX_DRAWINGS_PER_CHART:
            bucket.pop(0)
        return drawing

    def remove(self, symbol: str, timeframe: str, drawing_id: str) -> bool:
        bucket = self._charts.get(self.key(symbol, timeframe), [])
        for index, drawing in enumerate(bucket):
            if drawing.drawing_id == drawing_id:
                bucket.pop(index)
                return True
        return False

    def set_hidden(self, symbol: str, timeframe: str, drawing_id: str,
                   hidden: bool) -> bool:
        for drawing in self._charts.get(self.key(symbol, timeframe), []):
            if drawing.drawing_id == drawing_id:
                drawing.hidden = bool(hidden)
                return True
        return False

    def reset(self, symbol: str, timeframe: str) -> int:
        bucket = self._charts.pop(self.key(symbol, timeframe), [])
        return len(bucket)

    def charts(self) -> Dict[str, int]:
        return {key: len(value) for key, value in self._charts.items()}
