"""Is this engine allowed to have an opinion right now?

Every number in this package is computed from a feed, and a feed can be
connected-but-stale, connected-but-desynced, or reconnecting. None of
those look like an error from inside a calculation: a five-second-old
order book produces a perfectly well-formed imbalance that describes a
market that has moved on. So the question "is the data good" has to be
asked explicitly, by something that knows when each stream last spoke.

That is this module, and its answer gates the signal machine: OK produces
signals, DEGRADED does not. There is no cautious middle where a stale
feed produces a smaller signal, because a signal computed from stale data
is not smaller, it is wrong.

Latency here is exchange-to-server: the difference between the timestamp
Bybit stamped on a message and the local clock when it arrived. That
number includes any clock skew between the two machines and is reported
as a measurement rather than a guarantee.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from t3_engine.lead_engine.config import Thresholds

OK = "OK"
DEGRADED = "DEGRADED"
STARTING = "STARTING"
DISABLED = "DISABLED"

# Latency above this is reported but does not on its own degrade the
# engine - a slow link still delivers correct data in order.
LATENCY_WARN_MS = 2_000


@dataclass
class StreamHealth:
    """Per-symbol freshness. Timestamps are milliseconds, exchange clock
    where one exists and local otherwise (noted per field)."""

    symbol: str
    ws_connected: bool = False
    orderbook_synced: bool = False
    last_book_ms: int = 0
    last_trade_ms: int = 0
    last_ticker_ms: int = 0
    last_liquidation_ms: int = 0
    last_oi_ms: int = 0
    last_kline_ms: int = 0
    latency_ms: float = 0.0
    processing_ms: float = 0.0
    dropped_messages: int = 0
    reconnects: int = 0
    messages: int = 0

    def age_seconds(self, stamp_ms: int, now_ms: Optional[int] = None) -> Optional[float]:
        if not stamp_ms:
            return None
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        return max(0.0, (now_ms - stamp_ms) / 1000.0)

    def as_dict(self, now_ms: Optional[int] = None) -> Dict[str, object]:
        return {
            "symbol": self.symbol,
            "ws_connected": self.ws_connected,
            "orderbook_synced": self.orderbook_synced,
            "last_book_age_s": self.age_seconds(self.last_book_ms, now_ms),
            "last_trade_age_s": self.age_seconds(self.last_trade_ms, now_ms),
            "last_ticker_age_s": self.age_seconds(self.last_ticker_ms, now_ms),
            "last_liquidation_age_s": self.age_seconds(self.last_liquidation_ms, now_ms),
            "last_kline_age_s": self.age_seconds(self.last_kline_ms, now_ms),
            "oi_age_s": self.age_seconds(self.last_oi_ms, now_ms),
            "latency_ms": round(self.latency_ms, 1),
            "processing_ms": round(self.processing_ms, 3),
            "dropped_messages": self.dropped_messages,
            "reconnects": self.reconnects,
            "messages": self.messages,
        }


@dataclass
class HealthVerdict:
    status: str
    signals_enabled: bool
    reasons: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return {"status": self.status, "signals_enabled": self.signals_enabled,
                "reasons": list(self.reasons)}


def assess(health: StreamHealth, thresholds: Thresholds,
           now_ms: Optional[int] = None, started: bool = True) -> HealthVerdict:
    """The verdict for one symbol.

    Absence of liquidations is NOT a fault - most instruments go minutes
    without one, and treating that as degraded would mute the engine
    permanently on the quiet half of the symbol list. Absence of an ORDER
    BOOK is a fault, because the book updates continuously by
    construction."""
    if not started:
        return HealthVerdict(STARTING, False, ["engine has not finished starting"])

    reasons: List[str] = []
    if not health.ws_connected:
        reasons.append("websocket not connected")
    if not health.orderbook_synced:
        reasons.append("order book not synced")

    book_age = health.age_seconds(health.last_book_ms, now_ms)
    if book_age is None:
        reasons.append("no order book received yet")
    elif book_age > thresholds.max_book_age_seconds:
        reasons.append(f"order book {book_age:.1f}s old")

    trade_age = health.age_seconds(health.last_trade_ms, now_ms)
    if trade_age is not None and trade_age > thresholds.max_trade_age_seconds:
        reasons.append(f"no trade for {trade_age:.0f}s")

    ticker_age = health.age_seconds(health.last_ticker_ms, now_ms)
    if ticker_age is not None and ticker_age > thresholds.max_ticker_age_seconds:
        reasons.append(f"ticker {ticker_age:.0f}s old")

    if reasons:
        return HealthVerdict(DEGRADED, False, reasons)

    notes: List[str] = []
    oi_age = health.age_seconds(health.last_oi_ms, now_ms)
    if oi_age is not None and oi_age > thresholds.max_oi_age_seconds:
        # Stale open interest costs the engine one component out of nine,
        # not its whole opinion - so it is a note, not a degradation.
        notes.append(f"open interest {oi_age / 60:.0f}m old")
    if health.latency_ms > LATENCY_WARN_MS:
        notes.append(f"latency {health.latency_ms:.0f}ms")
    return HealthVerdict(OK, True, notes)
