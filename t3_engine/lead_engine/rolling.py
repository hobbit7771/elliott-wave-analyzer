"""Time-windowed accumulators, shared by the feature modules here.

Every number in this engine is "over the last N milliseconds", and the
windows the specification asks for run from 250ms to 5 minutes. Doing
that with one deque per window per symbol would be seven copies of the
same events; `TimeSeries` keeps ONE deque per stream and answers any
window by walking back from the newest entry.

Everything is keyed on the EXCHANGE timestamp, not on arrival time.
That matters for two different reasons and both bite: a 400ms network
hiccup would otherwise compress four seconds of trades into one window
and print a velocity spike that never happened, and replay (replay.py)
would produce different features from the same recorded events.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Iterable, List, Optional, Tuple

# The windows named in the specification, in milliseconds.
WINDOWS_MS: Tuple[int, ...] = (250, 1_000, 3_000, 5_000, 15_000, 30_000, 60_000, 300_000)

WINDOW_LABELS = {
    250: "250ms", 1_000: "1s", 3_000: "3s", 5_000: "5s",
    15_000: "15s", 30_000: "30s", 60_000: "60s", 300_000: "5m",
}


@dataclass
class TimeSeries:
    """Timestamped items, newest last, trimmed to `horizon_ms`.

    Every method is guarded by a lock, because this is genuinely
    cross-thread: the socket thread appends while a request thread walks
    the same deque to answer `/state`. Without it CPython raises
    `RuntimeError: deque mutated during iteration` and the endpoint
    500s - intermittently, under load, which is the worst way to find
    out. The critical sections are a single append or one pass over at
    most `max_items`, and readers run a few times a second against an
    ingest that runs thousands, so the contention is one-sided and
    small."""

    horizon_ms: int = 300_000
    max_items: int = 20_000

    def __post_init__(self) -> None:
        self._items: Deque[Tuple[int, object]] = deque()
        self._lock = threading.Lock()

    def add(self, timestamp_ms: int, item: object) -> None:
        # Out-of-order arrivals happen: Bybit interleaves topics and a
        # burst can deliver a trade stamped a few ms before one already
        # stored. Appending anyway keeps the series complete, and every
        # reader below tolerates a series that is not perfectly sorted
        # because it filters on the timestamp rather than on position.
        with self._lock:
            self._items.append((int(timestamp_ms), item))
            self._trim_locked()

    def _trim_locked(self) -> None:
        if not self._items:
            return
        newest = self._items[-1][0]
        cutoff = newest - self.horizon_ms
        while self._items and self._items[0][0] < cutoff:
            self._items.popleft()
        while len(self._items) > self.max_items:
            self._items.popleft()

    def _trim(self) -> None:
        with self._lock:
            self._trim_locked()

    def window(self, window_ms: int, now_ms: Optional[int] = None) -> List[object]:
        """Items stamped within `window_ms` of `now_ms` (default: newest)."""
        with self._lock:
            if not self._items:
                return []
            reference = self._items[-1][0] if now_ms is None else int(now_ms)
            cutoff = reference - int(window_ms)
            return [item for stamp, item in self._items
                    if cutoff <= stamp <= reference]

    def newest(self) -> Optional[object]:
        with self._lock:
            return self._items[-1][1] if self._items else None

    def newest_timestamp(self) -> Optional[int]:
        with self._lock:
            return self._items[-1][0] if self._items else None

    def oldest_timestamp(self) -> Optional[int]:
        with self._lock:
            return self._items[0][0] if self._items else None

    def all(self) -> List[object]:
        with self._lock:
            return [item for _, item in self._items]

    def stamped(self) -> List[Tuple[int, object]]:
        with self._lock:
            return list(self._items)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


def summed(items: Iterable[object], key: Callable[[object], float]) -> float:
    return float(sum(key(item) for item in items))


def mean(values: Iterable[float]) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def median(values: Iterable[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def stdev(values: Iterable[float]) -> float:
    values = list(values)
    if len(values) < 2:
        return 0.0
    average = mean(values)
    variance = sum((v - average) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(max(0.0, variance))


def zscore(value: float, history: Iterable[float]) -> float:
    """How unusual `value` is against `history`.

    Zero - not an arbitrary large number - when the history is too short
    or flat to say. A z-score computed from two samples is not a measure
    of anything, and returning one would let the signal machine fire on
    the third trade of a session."""
    history = list(history)
    if len(history) < 8:
        return 0.0
    spread = stdev(history)
    if spread <= 0:
        return 0.0
    return (value - mean(history)) / spread


def clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def scale_to_unit(value: float, full_scale: float) -> float:
    """Map a raw quantity onto -1..+1, saturating at `full_scale`.

    Used everywhere a component score is built. Saturation rather than
    normalisation by the running maximum: a single freak print would
    otherwise rescale every subsequent reading downward and quietly mute
    the engine for the rest of the session."""
    if full_scale <= 0:
        return 0.0
    return clamp(value / full_scale)
