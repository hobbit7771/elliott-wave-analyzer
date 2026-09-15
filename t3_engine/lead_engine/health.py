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
STALE_DATA = "STALE_DATA"
# The socket is up and frames are arriving, but what they carry is old.
# Named apart from DEGRADED because it is the case a header must never
# render as "feed OK": connected is not the same as current, and the
# first build showed the first while meaning the second.
WS_CONNECTED_DATA_STALE = "WS_CONNECTED_DATA_STALE"
STARTING = "STARTING"
DISABLED = "DISABLED"

# Latency above this is reported but does not on its own degrade the
# engine - a slow link still delivers correct data in order.
LATENCY_WARN_MS = 2_000


@dataclass
class StreamHealth:
    """Per-symbol freshness, with the four clocks kept apart.

    The first build had ONE number, `latency_ms`, and printed it twice
    under two names. The header showed "latency 3334ms" beside "book 3.3s"
    and those were the same staleness reported as if they were different
    measurements. They are not the same thing and conflating them hides
    the one case that matters - a fast link delivering old data.

    The four, precisely:

      ws_latency_ms       exchange stamp vs local clock at ARRIVAL,
                          measured only on frames as they land. Includes
                          clock skew between Bybit and this host, which
                          is why it is reported as a measurement rather
                          than a guarantee.
      book_age_ms         now vs the last book update's exchange stamp.
                          How old the book IS, which is the number that
                          decides whether signals may fire.
      trade_age_ms        the same for the trade stream.
      processing_ms       how long this process spent handling the last
                          frame. Pure local CPU, no network in it.

    A fifth, ui_latency_ms, is measured in the browser and reported back;
    the server cannot know it."""

    symbol: str
    ws_connected: bool = False
    orderbook_synced: bool = False
    # EXCHANGE timestamps - when Bybit says the event happened.
    last_book_ms: int = 0
    last_trade_ms: int = 0
    last_ticker_ms: int = 0
    last_liquidation_ms: int = 0
    last_oi_ms: int = 0
    last_kline_ms: int = 0
    # RECEIVE timestamps - when the frame landed on this host. Kept apart
    # from the exchange stamps because subtracting one from the other is
    # the network latency, and subtracting either from `now` answers a
    # different question. Conflating them is how "latency 3334ms" came to
    # be printed beside "book 3.3s" as if they were two measurements.
    last_book_receive_ms: int = 0
    last_trade_receive_ms: int = 0
    last_ticker_receive_ms: int = 0
    last_oi_receive_ms: int = 0
    # PROCESS timestamp - when this engine finished handling that frame.
    last_process_ms: int = 0
    # UI timestamp - when the browser last rendered a state. Reported by
    # the browser; the server cannot know it.
    last_ui_ms: int = 0
    # receive - exchange. The wire, plus any clock skew between the two
    # machines, which is why it is a measurement and not a guarantee.
    network_latency_ms: float = 0.0
    # process - receive. Pure local CPU, no network in it.
    processing_latency_ms: float = 0.0
    dropped_messages: int = 0
    reconnects: int = 0
    messages: int = 0
    sequence: Dict[str, object] = field(default_factory=dict)

    # ---- the old names, unchanged in meaning ----

    @property
    def ws_latency_ms(self) -> float:
        """The wire. Never the data's age."""
        return self.network_latency_ms

    @ws_latency_ms.setter
    def ws_latency_ms(self, value: float) -> None:
        self.network_latency_ms = float(value)

    @property
    def latency_ms(self) -> float:
        return self.network_latency_ms

    @latency_ms.setter
    def latency_ms(self, value: float) -> None:
        self.network_latency_ms = float(value)

    @property
    def processing_ms(self) -> float:
        return self.processing_latency_ms

    @processing_ms.setter
    def processing_ms(self, value: float) -> None:
        self.processing_latency_ms = float(value)

    # ---- ages, each measured from the clock that answers its question ----

    def book_age_ms_now(self, now_ms: Optional[int] = None) -> Optional[float]:
        """How long since a book frame LANDED. This is the number the
        freshness thresholds gate on: it asks whether this process is
        current, with the wire's contribution already accounted for
        separately as network latency."""
        return self.age_ms(self.last_book_receive_ms or self.last_book_ms, now_ms)

    def trade_age_ms_now(self, now_ms: Optional[int] = None) -> Optional[float]:
        return self.age_ms(self.last_trade_receive_ms or self.last_trade_ms, now_ms)

    def ui_age_ms_now(self, now_ms: Optional[int] = None) -> Optional[float]:
        return self.age_ms(self.last_ui_ms, now_ms)

    def age_seconds(self, stamp_ms: int, now_ms: Optional[int] = None) -> Optional[float]:
        if not stamp_ms:
            return None
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        return max(0.0, (now_ms - stamp_ms) / 1000.0)

    def age_ms(self, stamp_ms: int, now_ms: Optional[int] = None) -> Optional[float]:
        seconds = self.age_seconds(stamp_ms, now_ms)
        return None if seconds is None else round(seconds * 1000.0, 1)

    def as_dict(self, now_ms: Optional[int] = None) -> Dict[str, object]:
        return {
            "symbol": self.symbol,
            "ws_connected": self.ws_connected,
            "orderbook_synced": self.orderbook_synced,
            # Each named for what it actually measures. None of these is
            # called "latency" unless it IS a latency.
            "network_latency_ms": round(self.network_latency_ms, 1),
            "processing_latency_ms": round(self.processing_latency_ms, 3),
            "ws_latency_ms": round(self.network_latency_ms, 1),
            "book_age_ms": self.book_age_ms_now(now_ms),
            "trade_age_ms": self.trade_age_ms_now(now_ms),
            "ui_age_ms": self.ui_age_ms_now(now_ms),
            # Total staleness including the wire: now vs the EXCHANGE
            # stamp. Reported beside the receive-based age rather than
            # instead of it, because they answer different questions.
            "book_data_age_ms": self.age_ms(self.last_book_ms, now_ms),
            "trade_data_age_ms": self.age_ms(self.last_trade_ms, now_ms),
            "ticker_age_ms": self.age_ms(self.last_ticker_ms, now_ms),
            "liquidation_age_ms": self.age_ms(self.last_liquidation_ms, now_ms),
            "kline_age_ms": self.age_ms(self.last_kline_ms, now_ms),
            "oi_age_ms": self.age_ms(self.last_oi_ms, now_ms),
            "processing_ms": round(self.processing_latency_ms, 3),
            "sequence": dict(self.sequence),
            # The same ages in seconds, kept because the first version of
            # the tab reads them.
            "last_book_age_s": self.age_seconds(self.last_book_ms, now_ms),
            "last_trade_age_s": self.age_seconds(self.last_trade_ms, now_ms),
            "last_ticker_age_s": self.age_seconds(self.last_ticker_ms, now_ms),
            "last_liquidation_age_s": self.age_seconds(self.last_liquidation_ms, now_ms),
            "last_kline_age_s": self.age_seconds(self.last_kline_ms, now_ms),
            "oi_age_s": self.age_seconds(self.last_oi_ms, now_ms),
            "latency_ms": round(self.ws_latency_ms, 1),
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
    structural = False
    if not health.ws_connected:
        reasons.append("websocket not connected")
        structural = True
    if not health.orderbook_synced:
        detail = (health.sequence or {}).get("desync_reason") or ""
        reasons.append("order book not synced" + (f": {detail}" if detail else ""))
        structural = True

    # Two levels, two different decisions. `degraded` means the reading
    # is shown but not to be trusted; `mute` means the engine has no
    # opinion at all. Both measured from when the frame LANDED, with the
    # wire accounted for separately as network latency.
    degraded = False
    # No socket, or a book that cannot be trusted, is not a matter of
    # degree: there is nothing to have an opinion about.
    mute = structural
    # Observations that are worth reporting but are not faults.
    quiet: List[str] = []

    book_age = health.book_age_ms_now(now_ms)
    if book_age is None:
        reasons.append("no order book received yet")
        mute = True
    else:
        if book_age > thresholds.book_age_signals_off_ms:
            reasons.append(f"order book {book_age / 1000.0:.1f}s old "
                           f"(> {thresholds.book_age_signals_off_ms / 1000.0:.1f}s)")
            mute = True
        elif book_age > thresholds.book_age_degraded_ms:
            reasons.append(f"order book {book_age / 1000.0:.1f}s old "
                           f"(> {thresholds.book_age_degraded_ms / 1000.0:.1f}s)")
            degraded = True

    # A QUIET TAPE IS NOT A BROKEN FEED.
    #
    # The first version muted on trade age alone, and the thirty-minute
    # live run showed what that costs: between one and six of seven
    # symbols sat in DATA_FAILURE continuously while the socket was
    # connected, had never reconnected, and had zero sequence gaps. The
    # book was a hundred milliseconds old. Nothing was wrong - ATOMUSDT
    # and FILUSDT simply go more than a second and a half without a
    # print, which is ordinary behaviour for them and not a fault to
    # report.
    #
    # So a trade gap only counts against the feed when the BOOK is stale
    # too. If book updates keep arriving, the connection is demonstrably
    # alive and the instrument is merely quiet; the flow layer then has
    # fewer inputs to answer with and its own confidence falls, which is
    # the honest way for a silent tape to reach the signal machine - see
    # layers._combine and MIN_DIRECTIONAL_CONFIDENCE.
    trade_age = health.trade_age_ms_now(now_ms)
    book_is_fresh = book_age is not None and \
        book_age <= thresholds.book_age_degraded_ms
    if trade_age is not None and trade_age > thresholds.trade_age_signals_off_ms:
        if trade_age > thresholds.trade_age_dead_ms:
            # Quiet is one thing; silent for five minutes while the book
            # keeps ticking is a dead subscription.
            reasons.append(f"no trade for {trade_age / 60_000.0:.0f}m while the "
                           f"book is live - the trade feed looks dead")
            mute = True
        elif book_is_fresh:
            quiet.append(f"no trade for {trade_age / 1000.0:.1f}s - quiet tape, "
                         f"book is {book_age:.0f}ms old")
        else:
            reasons.append(f"no trade for {trade_age / 1000.0:.1f}s "
                           f"(> {thresholds.trade_age_signals_off_ms / 1000.0:.1f}s) "
                           f"and the book is stale too")
            mute = True

    ticker_age = health.age_seconds(health.last_ticker_ms, now_ms)
    if ticker_age is not None and ticker_age > thresholds.max_ticker_age_seconds:
        reasons.append(f"ticker {ticker_age:.0f}s old")
        degraded = True

    if reasons:
        if not health.ws_connected:
            # No socket at all. A different fault from a socket that is up
            # and delivering old data, and named differently.
            status = DEGRADED
        elif mute or degraded:
            # Frames ARE arriving and what they carry is old, or the book
            # they describe cannot be trusted. This is the case a header
            # must never render as "feed OK".
            status = WS_CONNECTED_DATA_STALE
        else:
            status = DEGRADED
        return HealthVerdict(status, not mute and not degraded, reasons)

    notes: List[str] = list(quiet)
    oi_age = health.age_seconds(health.last_oi_ms, now_ms)
    if oi_age is not None and oi_age > thresholds.max_oi_age_seconds:
        # Stale open interest costs the engine one component out of nine,
        # not its whole opinion - so it is a note, not a degradation.
        notes.append(f"open interest {oi_age / 60:.0f}m old")
    if health.network_latency_ms > LATENCY_WARN_MS:
        notes.append(f"network latency {health.network_latency_ms:.0f}ms")
    return HealthVerdict(OK, True, notes)
