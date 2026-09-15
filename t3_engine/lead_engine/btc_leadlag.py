"""Does BTC move first, and by how much?

Crypto perpetuals are not independent instruments. When BTC turns, most
of the book turns with it, and the useful question is not "are they
correlated" - they are - but "is BTC ALREADY doing the thing INJ is about
to do". That is a lag question, and it needs three pieces:

  - A common time grid. Trades arrive whenever they arrive, so returns
    are bucketed into fixed intervals (default one second) before anything
    is compared. Comparing raw trade-to-trade returns across two
    instruments with different trade rates measures the trade rates.
  - A correlation, over a rolling window of those buckets.
  - A cross-correlation over candidate lags, to find which shift of the
    altcoin against BTC lines up best. The argmax is the lag estimate.

The lag is reported in milliseconds and is deliberately coarse: with
one-second buckets, a lag estimate of 3000ms means "about three buckets",
not three thousand milliseconds of precision. The UI prints it as buckets
for that reason.

BTC impulse is separate from all of that: it is what BTC is doing RIGHT
NOW, normalised by its own recent volatility, and it is what actually
carries information forward. A high correlation with a BTC that is not
moving tells you nothing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from t3_engine.lead_engine.rolling import TimeSeries, clamp, mean, stdev

# Returns are bucketed onto this grid before any comparison.
BUCKET_MS = 1_000

# How many buckets the correlation and the lag search look back over.
CORRELATION_BUCKETS = 120

# Candidate lags, in buckets. Only positive: the question is whether BTC
# leads, and an altcoin "leading" BTC over seconds is noise, not a finding.
MAX_LAG_BUCKETS = 8

# A correlation below this is not strong enough for a lag estimate to
# mean anything, so none is offered.
MIN_ABS_CORRELATION = 0.25

BULLISH = "BTC_LEAD_BULLISH"
BEARISH = "BTC_LEAD_BEARISH"
NEUTRAL = "BTC_LEAD_NEUTRAL"

# BTC impulse, in standard deviations of its own recent bucket returns,
# that counts as a full-strength lead.
IMPULSE_FULL_SCALE = 2.5


@dataclass
class ReturnGrid:
    """Last price per time bucket, and the returns between them."""

    symbol: str
    prices: Dict[int, float] = field(default_factory=dict)
    _order: List[int] = field(default_factory=list)

    def observe(self, timestamp_ms: int, price: float) -> None:
        if price <= 0:
            return
        bucket = int(timestamp_ms) // BUCKET_MS
        if bucket not in self.prices:
            self._order.append(bucket)
        self.prices[bucket] = float(price)
        # Bounded: only the correlation window plus the lag search is ever
        # read, and this runs inside a long-lived web process.
        limit = CORRELATION_BUCKETS + MAX_LAG_BUCKETS + 10
        while len(self._order) > limit:
            self.prices.pop(self._order.pop(0), None)

    def buckets(self) -> List[int]:
        return sorted(self.prices)

    def returns(self) -> Dict[int, float]:
        """Log return per bucket, keyed by the LATER bucket.

        Log returns because they add across buckets and are symmetric; a
        percentage change is neither, and both matter when the same series
        is shifted against itself at several lags."""
        ordered = self.buckets()
        out: Dict[int, float] = {}
        for previous, current in zip(ordered, ordered[1:]):
            before, after = self.prices[previous], self.prices[current]
            if before > 0 and after > 0:
                out[current] = math.log(after / before)
        return out

    def newest_bucket(self) -> Optional[int]:
        return max(self.prices) if self.prices else None


def correlation(left: List[float], right: List[float]) -> Optional[float]:
    if len(left) != len(right) or len(left) < 10:
        return None
    left_sd, right_sd = stdev(left), stdev(right)
    if left_sd <= 0 or right_sd <= 0:
        return None
    left_mean, right_mean = mean(left), mean(right)
    covariance = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    covariance /= (len(left) - 1)
    return covariance / (left_sd * right_sd)


def aligned(btc: Dict[int, float], alt: Dict[int, float],
            lag_buckets: int, window: int = CORRELATION_BUCKETS) -> Tuple[List[float], List[float]]:
    """BTC's return at bucket b-lag against the altcoin's at bucket b.

    Positive `lag_buckets` therefore means BTC moved FIRST, which is the
    only direction this module reports on."""
    if not alt:
        return [], []
    newest = max(alt)
    left: List[float] = []
    right: List[float] = []
    for bucket in range(newest - window + 1, newest + 1):
        btc_value = btc.get(bucket - lag_buckets)
        alt_value = alt.get(bucket)
        if btc_value is None or alt_value is None:
            continue
        left.append(btc_value)
        right.append(alt_value)
    return left, right


@dataclass
class LeadLagState:
    """One altcoin's relationship to BTC."""

    symbol: str
    correlation: Optional[float] = None
    lag_buckets: Optional[int] = None
    lag_ms: Optional[int] = None
    btc_direction: str = "flat"
    btc_impulse: float = 0.0
    lead_state: str = NEUTRAL
    lead_score: float = 0.0

    def as_dict(self) -> Dict[str, object]:
        return {
            "correlation": None if self.correlation is None else round(self.correlation, 4),
            "lag_buckets": self.lag_buckets,
            "estimated_lag_ms": self.lag_ms,
            "btc_direction": self.btc_direction,
            "btc_impulse": round(self.btc_impulse, 4),
            "btc_lead_state": self.lead_state,
            "btc_lead_score": round(self.lead_score, 4),
        }


class BtcLeadLag:
    """Holds the BTC grid and one grid per altcoin."""

    def __init__(self, btc_symbol: str = "BTCUSDT") -> None:
        self.btc_symbol = btc_symbol.upper()
        self.grids: Dict[str, ReturnGrid] = {}
        self.btc_returns_history: TimeSeries = TimeSeries(horizon_ms=600_000)

    def _grid(self, symbol: str) -> ReturnGrid:
        symbol = symbol.upper()
        if symbol not in self.grids:
            self.grids[symbol] = ReturnGrid(symbol)
        return self.grids[symbol]

    def observe(self, symbol: str, timestamp_ms: int, price: float) -> None:
        self._grid(symbol).observe(timestamp_ms, price)

    # ---- BTC's own state ----

    def btc_impulse(self) -> Tuple[str, float]:
        """BTC's last few seconds, in standard deviations of its own noise.

        Returns (direction, normalised magnitude). Normalising matters:
        a 0.1% BTC move is enormous in a quiet hour and unremarkable in a
        volatile one, and an unnormalised threshold would fire on the
        clock rather than on the market."""
        returns = self._grid(self.btc_symbol).returns()
        if len(returns) < 12:
            return "flat", 0.0
        ordered = [returns[b] for b in sorted(returns)]
        recent = ordered[-3:]
        baseline = ordered[:-3] or ordered
        spread = stdev(baseline)
        total = sum(recent)
        if spread <= 0:
            return "flat", 0.0
        normalised = total / (spread * math.sqrt(len(recent)))
        if normalised > 0.5:
            return "up", normalised
        if normalised < -0.5:
            return "down", normalised
        return "flat", normalised

    # ---- the per-symbol reading ----

    def state_for(self, symbol: str) -> LeadLagState:
        symbol = symbol.upper()
        out = LeadLagState(symbol=symbol)
        direction, impulse = self.btc_impulse()
        out.btc_direction, out.btc_impulse = direction, impulse

        if symbol == self.btc_symbol:
            # BTC does not lead itself. Reported as such rather than as a
            # correlation of 1.0, which would be true and useless.
            out.correlation, out.lag_buckets, out.lag_ms = None, None, None
            out.lead_state = NEUTRAL
            out.lead_score = 0.0
            return out

        btc_returns = self._grid(self.btc_symbol).returns()
        alt_returns = self._grid(symbol).returns()
        best_corr: Optional[float] = None
        best_lag: Optional[int] = None
        for lag in range(0, MAX_LAG_BUCKETS + 1):
            left, right = aligned(btc_returns, alt_returns, lag)
            value = correlation(left, right)
            if value is None:
                continue
            if best_corr is None or abs(value) > abs(best_corr):
                best_corr, best_lag = value, lag
        out.correlation = best_corr
        if best_corr is not None and abs(best_corr) >= MIN_ABS_CORRELATION:
            out.lag_buckets = best_lag
            out.lag_ms = (best_lag or 0) * BUCKET_MS
        else:
            # Honest absence. A lag read off a correlation of 0.05 is the
            # argmax of noise.
            out.lag_buckets = None
            out.lag_ms = None

        out.lead_score = self._lead_score(out)
        if out.lead_score > 0.15:
            out.lead_state = BULLISH
        elif out.lead_score < -0.15:
            out.lead_state = BEARISH
        else:
            out.lead_state = NEUTRAL
        return out

    @staticmethod
    def _lead_score(state: LeadLagState) -> float:
        """-1..+1: how much BTC's current move should be expected to carry.

        Three factors multiplied, because all three are necessary: BTC has
        to be moving, the two have to be related, and the relationship has
        to be one where BTC is ahead. Any one of them missing makes the
        other two irrelevant, which multiplication expresses and a
        weighted sum does not."""
        if state.correlation is None:
            return 0.0
        strength = clamp(state.btc_impulse / IMPULSE_FULL_SCALE)
        relatedness = clamp(abs(state.correlation))
        if relatedness < MIN_ABS_CORRELATION:
            return 0.0
        # A negative correlation means the altcoin moves the other way, so
        # the expected carry flips with it.
        sign = 1.0 if state.correlation >= 0 else -1.0
        # A lag of zero is simultaneous movement, which is still
        # information but less of it than BTC being demonstrably ahead.
        lead_weight = 0.6 if not state.lag_buckets else 1.0
        return clamp(strength * relatedness * sign * lead_weight)

    def pressure_component(self, symbol: str) -> float:
        return self.state_for(symbol).lead_score

    def as_dict(self, symbol: str) -> Dict[str, object]:
        return self.state_for(symbol).as_dict()
