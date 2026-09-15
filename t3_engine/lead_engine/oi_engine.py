"""Open interest, polled from Bybit's REST API on its own clock.

Open interest is not on the WebSocket as a stream of its own, so it has
to be pulled. Two rules follow from that and both are in the
specification for good reason:

  - It never runs on the socket loop. A REST call that stalls for thirty
    seconds must not also stall the order book, so `OpenInterestPoller`
    owns a thread and the socket never waits on it.
  - It is not a price. OI is reported on a five-minute grid, so it is
    context - what the LAST few minutes of positioning did - and the four
    interpretations below are the whole of its contribution. Treating a
    five-minute-old number as a tick would be the lookahead's opposite:
    stale data presented as current.

The four readings are the standard ones and they are not symmetrical
guesses, they are accounting:

  price up,   OI up    new longs opening - expansion
  price up,   OI down  shorts covering - a squeeze, not new demand
  price down, OI up    new shorts opening
  price down, OI down  longs closing - deleveraging

The distinction that matters most is the second: a rally on FALLING open
interest is people buying back what they sold, and it stops when they are
done.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import httpx

from t3_engine.lead_engine.rolling import TimeSeries, clamp, scale_to_unit

logger = logging.getLogger(__name__)

EXPANSION = "new_longs_expansion"
SHORT_COVERING = "short_covering"
NEW_SHORTS = "new_shorts"
DELEVERAGING = "long_liquidation_deleveraging"
UNKNOWN = "unknown"

# Percent change in OI below which nothing is claimed either way.
FLAT_PCT = 0.05

# The OI change, in percent over the trend window, that counts as a
# full-strength reading for the pressure score.
FULL_SCALE_PCT = 2.0


@dataclass
class OpenInterestState:
    symbol: str
    series: TimeSeries = field(default_factory=lambda: TimeSeries(horizon_ms=6 * 3_600_000))
    price_series: TimeSeries = field(default_factory=lambda: TimeSeries(horizon_ms=6 * 3_600_000))
    last_error: str = ""
    polls: int = 0
    failures: int = 0
    # The five-minute bucket this state last recorded, so a repeat poll of
    # the same bucket is not stored as a second reading.
    last_bucket_ms: int = 0
    last_value: Optional[float] = None
    buckets: int = 0
    repeats: int = 0
    revisions: int = 0

    def observe(self, timestamp_ms: int, open_interest: float,
                price: Optional[float] = None) -> None:
        """One reading. Repeat polls of the SAME five-minute bucket are
        recorded once.

        Bybit publishes open interest on a five-minute grid and this
        poller runs every sixty seconds, so four polls out of five return
        the row already held - identical timestamp, identical value.
        Storing each of them made a window of "the last fifteen minutes"
        hold twelve copies of three readings, which is how a delta of
        zero came to mean "polled again" rather than "did not change".

        A bucket whose value is later revised still wins: the newest
        reading for a timestamp replaces the older one."""
        stamp = int(timestamp_ms)
        value = float(open_interest)
        if stamp and stamp == self.last_bucket_ms:
            if value == self.last_value:
                # Nothing new. The price series is deliberately NOT
                # touched: the four interpretations compare the price
                # change against the OI change over the same buckets, so
                # both have to be sampled on the same clock. Mixing a
                # wall-clock sample into a series stamped with exchange
                # buckets makes the window straddle two clocks and the
                # comparison meaningless.
                self.repeats += 1
                return
            self.revisions += 1                  # same bucket, new number
            self._replace_newest(stamp, value)
        else:
            self.series.add(stamp, value)
            self.buckets += 1
        self.last_bucket_ms = stamp
        self.last_value = value
        if price is not None:
            self.price_series.add(stamp, float(price))

    def _replace_newest(self, stamp: int, value: float) -> None:
        kept = [(t, v) for t, v in self.series.stamped() if t != stamp]
        self.series = TimeSeries(horizon_ms=self.series.horizon_ms)
        for t, v in kept:
            self.series.add(t, v)
        self.series.add(stamp, value)

    @property
    def previous(self) -> Optional[float]:
        """The reading before the current one, or None when there is only
        one. Not the same thing as zero change."""
        values = self.series.all()
        return float(values[-2]) if len(values) >= 2 else None

    def delta_vs_previous(self) -> Optional[float]:
        current, previous = self.current, self.previous
        if current is None or previous is None:
            return None
        return current - previous

    def why_no_delta(self, window_ms: int = 900_000) -> str:
        """Said out loud, because "0" and "cannot say" are different
        answers and printing the first for the second is a lie."""
        values = self.series.window(window_ms)
        if not values:
            return "no open interest reading yet"
        if len(values) < 2:
            minutes = max(1, int(window_ms / 60_000))
            return (f"only one reading in the last {minutes}m - Bybit publishes "
                    f"open interest every 5 minutes, so the second is still coming")
        return ""

    @property
    def current(self) -> Optional[float]:
        value = self.series.newest()
        return float(value) if value is not None else None

    def updated_at_ms(self) -> Optional[int]:
        return self.series.newest_timestamp()

    def delta(self, window_ms: int = 900_000) -> Optional[float]:
        values = self.series.window(window_ms)
        if len(values) < 2:
            return None
        return float(values[-1]) - float(values[0])

    def delta_pct(self, window_ms: int = 900_000) -> Optional[float]:
        values = self.series.window(window_ms)
        if len(values) < 2 or float(values[0]) == 0:
            return None
        return (float(values[-1]) - float(values[0])) / float(values[0]) * 100.0

    def trend(self, window_ms: int = 900_000) -> str:
        change = self.delta_pct(window_ms)
        if change is None:
            return UNKNOWN
        if change > FLAT_PCT:
            return "rising"
        if change < -FLAT_PCT:
            return "falling"
        return "flat"

    def price_change(self, window_ms: int = 900_000) -> Optional[float]:
        values = self.price_series.window(window_ms)
        if len(values) < 2:
            return None
        return float(values[-1]) - float(values[0])

    def interpretation(self, window_ms: int = 900_000) -> str:
        oi_change = self.delta_pct(window_ms)
        price_change = self.price_change(window_ms)
        if oi_change is None or price_change is None:
            return UNKNOWN
        if abs(oi_change) <= FLAT_PCT or price_change == 0:
            return UNKNOWN
        if price_change > 0:
            return EXPANSION if oi_change > 0 else SHORT_COVERING
        return NEW_SHORTS if oi_change > 0 else DELEVERAGING

    def pressure_component(self, window_ms: int = 900_000) -> float:
        """-1..+1. Positioning, signed by what it means rather than by the
        direction of open interest itself: rising OI is bullish when price
        is rising and bearish when it is falling, because the same number
        is new longs in one case and new shorts in the other."""
        reading = self.interpretation(window_ms)
        magnitude = abs(scale_to_unit(self.delta_pct(window_ms) or 0.0, FULL_SCALE_PCT))
        if reading == EXPANSION:
            return clamp(magnitude)
        if reading == NEW_SHORTS:
            return clamp(-magnitude)
        # A squeeze and a deleveraging are moves without new conviction
        # behind them, so they count against the direction they travel in,
        # at half weight.
        if reading == SHORT_COVERING:
            return clamp(-0.5 * magnitude)
        if reading == DELEVERAGING:
            return clamp(0.5 * magnitude)
        return 0.0

    def as_dict(self) -> Dict[str, object]:
        return {
            "open_interest": self.current,
            "previous": self.previous,
            "oi_delta": self.delta(),
            "oi_delta_pct": self.delta_pct(),
            "oi_delta_vs_previous": self.delta_vs_previous(),
            "oi_trend": self.trend(),
            "interpretation": self.interpretation(),
            "updated_at_ms": self.updated_at_ms(),
            # Why there is no delta, when there is none. Empty when there
            # is one - including when the real answer is zero.
            "no_delta_reason": self.why_no_delta(),
            "readings": len(self.series),
            "buckets": self.buckets,
            "repeat_polls": self.repeats,
            "revisions": self.revisions,
            "polls": self.polls, "failures": self.failures,
            "last_error": self.last_error,
        }


def fetch_open_interest(symbol: str, base_url: str,
                        client: Optional[httpx.Client] = None,
                        timeout: float = 10.0) -> Optional[Dict[str, float]]:
    """One reading from Bybit v5. Returns None rather than raising.

    Bybit only: there is no Binance fallback here and there must not be
    one - see docs/lead-engine/BYBIT_STREAMS.md."""
    http = client or httpx.Client(timeout=timeout)
    try:
        resp = http.get(f"{base_url.rstrip('/')}/v5/market/open-interest",
                        params={"category": "linear", "symbol": symbol,
                                "intervalTime": "5min", "limit": 1})
        if resp.status_code != 200:
            return None
        payload = resp.json()
        rows = ((payload or {}).get("result") or {}).get("list") or []
        if not rows:
            return None
        row = rows[0]
        return {"open_interest": float(row.get("openInterest") or 0.0),
                "timestamp_ms": int(row.get("timestamp") or 0)}
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        return None
    finally:
        if client is None:
            http.close()


class OpenInterestPoller:
    """A thread that refreshes open interest and touches nothing else.

    Its own thread on purpose: the specification asks for polling to stay
    off the event loop, and the reason is concrete - an exchange REST
    endpoint that hangs would otherwise hold up the order book for as long
    as the socket coroutine waited on it."""

    def __init__(self, symbols: List[str], base_url: str,
                 on_reading: Callable[[str, float, int], None],
                 interval_seconds: float = 60.0,
                 fetcher: Optional[Callable[[str, str], Optional[Dict[str, float]]]] = None) -> None:
        self.symbols = [s.upper() for s in symbols]
        self.base_url = base_url
        self.on_reading = on_reading
        self.interval_seconds = max(5.0, float(interval_seconds))
        self._fetch = fetcher or (lambda symbol, base: fetch_open_interest(symbol, base))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="lead-engine-oi", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def poll_once(self) -> int:
        """One sweep of every symbol. Exposed so a test - and replay - can
        drive it without a thread or a clock."""
        delivered = 0
        for symbol in self.symbols:
            reading = self._fetch(symbol, self.base_url)
            if not reading:
                continue
            try:
                self.on_reading(symbol, float(reading["open_interest"]),
                                int(reading["timestamp_ms"]))
                delivered += 1
            except Exception:                   # noqa: BLE001
                logger.exception("lead_engine: open interest callback failed for %s", symbol)
        return delivered

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:                   # noqa: BLE001 - a poller that
                logger.exception("lead_engine: open interest sweep failed")  # dies is worse
            self._stop.wait(self.interval_seconds)
