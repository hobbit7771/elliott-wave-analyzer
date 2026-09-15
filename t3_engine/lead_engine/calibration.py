"""Turning a model score into a probability, or admitting it is not one.

The pre-break engine produces a weighted mean of ten features times a
compression gate. The first build called that `break_probability` and put
it on screen next to a percent sign. It is not a probability. Nothing had
been measured; a score of 70 did not mean seven in ten of anything.

This module is the difference between the two words:

  MODEL SCORE   what the features add up to. Always available.
  PROBABILITY   the share of PAST cases at this score that actually broke
                within a horizon. Available only once enough of them have
                been observed, and absent - not estimated - until then.

How it works. Every time a score crosses the attention threshold, an
observation is opened: the score, the level, the direction, the price and
the time. Prices arriving afterwards resolve it at 5, 15 and 30 seconds.
Resolved observations go into score buckets, and the probability for a new
score is the hit rate of its bucket.

Causality is the whole point of the design, so it is worth being explicit:
an observation is opened BEFORE its outcome exists and can only ever be
resolved by prices stamped after it. `resolve()` refuses a price at or
before the observation's own timestamp. There is no path by which a later
price changes the score that was recorded, and a test asserts it.

Until a bucket has MIN_SAMPLES resolved cases, `probability()` returns
None and the caller must keep saying MODEL SCORE. That is the honest
state for a young engine and it is the state it will be in for a while.
"""

from __future__ import annotations


from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

# Score buckets, ten points wide. Wide enough to fill in reasonable time,
# narrow enough that the answer means something.
BUCKET_WIDTH = 10.0

# The horizons the brief names.
HORIZONS_MS = (5_000, 15_000, 30_000)
HORIZON_LABELS = {5_000: "5s", 15_000: "15s", 30_000: "30s"}

# Below this many resolved cases in a bucket, no probability is offered.
# Thirty is not a lot of statistics; it is the point below which the
# number would be actively misleading rather than merely rough.
MIN_SAMPLES = 30

# Nothing below this score is worth recording - the engine is not
# claiming anything there and the buckets would fill with noise.
MIN_SCORE_TRACKED = 30.0

# How many resolved observations are kept per symbol.
MAX_OBSERVATIONS = 5_000

# How long an unresolved observation waits before being abandoned.
MAX_PENDING_MS = 120_000


def bucket_of(score: float) -> int:
    """The bucket a score falls in, as its lower bound."""
    return int(max(0.0, min(100.0, float(score))) // BUCKET_WIDTH) * int(BUCKET_WIDTH)


@dataclass
class Observation:
    """One score, recorded before its outcome existed."""

    symbol: str
    direction: str
    score: float
    level: float
    price: float
    opened_ms: int
    outcomes: Dict[int, Optional[bool]] = field(default_factory=dict)
    resolved_ms: Dict[int, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for horizon in HORIZONS_MS:
            self.outcomes.setdefault(horizon, None)

    @property
    def settled(self) -> bool:
        return all(value is not None for value in self.outcomes.values())

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "direction": self.direction,
            "score": round(self.score, 2), "level": self.level, "price": self.price,
            "opened_ms": self.opened_ms,
            "outcomes": {HORIZON_LABELS[h]: v for h, v in self.outcomes.items()},
        }


@dataclass
class Calibrator:
    """One symbol's record of what its scores have been worth."""

    symbol: str
    break_pct: float = 0.002
    pending: List[Observation] = field(default_factory=list)
    resolved: Deque[Observation] = field(default_factory=lambda: deque(maxlen=MAX_OBSERVATIONS))
    opened = 0
    abandoned = 0

    # ---- recording ----

    def observe(self, direction: str, score: float, level: Optional[float],
                price: Optional[float], timestamp_ms: int) -> Optional[Observation]:
        """Open an observation, if this score is worth tracking.

        Deduplicated on (direction, bucket): a score sitting at 64 for a
        minute is ONE case, not two hundred. Without that the buckets fill
        with copies of whichever setup lasted longest and the hit rate
        becomes a measure of persistence."""
        if score < MIN_SCORE_TRACKED or not level or not price or price <= 0:
            return None
        bucket = bucket_of(score)
        for existing in self.pending:
            if existing.direction == direction and bucket_of(existing.score) == bucket:
                return None
        observation = Observation(symbol=self.symbol, direction=direction,
                                  score=float(score), level=float(level),
                                  price=float(price), opened_ms=int(timestamp_ms))
        self.pending.append(observation)
        self.opened += 1
        return observation

    def resolve(self, price: float, timestamp_ms: int) -> int:
        """Settle pending observations against a price that arrived LATER.

        A price stamped at or before an observation's own moment is
        refused for that observation - that is the no-lookahead boundary,
        and it is enforced here rather than assumed."""
        settled = 0
        timestamp_ms = int(timestamp_ms)
        still_pending: List[Observation] = []
        for observation in self.pending:
            if timestamp_ms <= observation.opened_ms:
                still_pending.append(observation)
                continue
            elapsed = timestamp_ms - observation.opened_ms
            target = observation.level
            if observation.direction == "short":
                broke = price <= target * (1.0 - self.break_pct)
            else:
                broke = price >= target * (1.0 + self.break_pct)
            for horizon in HORIZONS_MS:
                if observation.outcomes[horizon] is not None:
                    continue
                if broke and elapsed <= horizon:
                    observation.outcomes[horizon] = True
                    observation.resolved_ms[horizon] = timestamp_ms
                elif elapsed > horizon:
                    observation.outcomes[horizon] = False
                    observation.resolved_ms[horizon] = timestamp_ms
            if observation.settled:
                self.resolved.append(observation)
                settled += 1
            elif timestamp_ms - observation.opened_ms > MAX_PENDING_MS:
                self.abandoned += 1
            else:
                still_pending.append(observation)
        self.pending = still_pending
        return settled

    # ---- reading ----

    def table(self, direction: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """Hit rate per score bucket per horizon, with sample counts.

        The counts travel with the rates because a rate without its N is
        not a finding."""
        out: Dict[str, Dict[str, Any]] = {}
        for observation in self.resolved:
            if direction and observation.direction != direction:
                continue
            key = str(bucket_of(observation.score))
            row = out.setdefault(key, {"samples": 0})
            row["samples"] += 1
            for horizon, outcome in observation.outcomes.items():
                label = HORIZON_LABELS[horizon]
                hits = row.get(f"{label}_hits", 0)
                seen = row.get(f"{label}_n", 0)
                row[f"{label}_hits"] = hits + (1 if outcome else 0)
                row[f"{label}_n"] = seen + 1
        for row in out.values():
            for label in HORIZON_LABELS.values():
                seen = row.get(f"{label}_n", 0)
                row[f"{label}_rate"] = (round(row.get(f"{label}_hits", 0) / seen, 4)
                                        if seen >= MIN_SAMPLES else None)
        return out

    def probability(self, score: float, horizon_ms: int = 15_000,
                    direction: Optional[str] = None) -> Optional[float]:
        """The empirical hit rate for this score, or None.

        None is the correct answer for a young engine and is returned
        rather than a smoothed guess. A caller receiving None must present
        the number as MODEL SCORE."""
        if horizon_ms not in HORIZON_LABELS:
            return None
        label = HORIZON_LABELS[horizon_ms]
        row = self.table(direction).get(str(bucket_of(score)))
        if not row:
            return None
        rate = row.get(f"{label}_rate")
        return float(rate) if rate is not None else None

    def summary(self) -> Dict[str, Any]:
        ready = {}
        for horizon, label in HORIZON_LABELS.items():
            buckets = {key: row[f"{label}_rate"] for key, row in self.table().items()
                       if row.get(f"{label}_rate") is not None}
            ready[label] = buckets
        return {
            "symbol": self.symbol,
            "break_pct": self.break_pct,
            "opened": self.opened,
            "pending": len(self.pending),
            "resolved": len(self.resolved),
            "abandoned": self.abandoned,
            "min_samples_for_a_rate": MIN_SAMPLES,
            "calibrated": any(ready.values()),
            "rates": ready,
            "table": self.table(),
        }


def label_for(score: float, probability: Optional[float]) -> Dict[str, Any]:
    """How a score should be presented, given whether it is calibrated.

    One place, so the API, the UI and the MCP adapter cannot disagree
    about whether the number on screen is a probability."""
    if probability is None:
        return {"kind": "MODEL_SCORE", "model_score": round(float(score), 2),
                "probability": None,
                "note": ("Not calibrated yet: this is the weighted feature score, "
                         "not an empirical probability. It becomes one once "
                         f"{MIN_SAMPLES} cases in this score bucket have resolved.")}
    return {"kind": "PROBABILITY", "model_score": round(float(score), 2),
            "probability": round(float(probability) * 100.0, 2),
            "note": "Empirical: the share of past cases in this score bucket that broke."}
