"""The ingest pipeline: the socket, the queue, the worker, the resync.

BUILD-CHECK-044. What these tests pin down was measured in production,
not imagined: the age of each processed frame climbed 0.2s -> 5s -> 34s
over about ten minutes until Bybit closed the connection with 1011
"keepalive ping timeout", and every downstream symptom - a stale book, a
header reading "feed OK" beside a multi-second age, signals computed from
half-minute-old data - was that one backlog.

Two causes, both fixed here and both tested:

  1. The message handler ran INLINE on the socket's event loop, so the
     scoring work and the keepalive shared a thread.
  2. `metrics()` - OBI at five depths, weighted OBI, walls, absorption,
     stacking - was computed on EVERY delta to get two numbers out of it,
     and sorted the whole book about fifty times per call.
"""

import json
import time

import pytest

from t3_engine.lead_engine.bybit_ws import (
    QUEUE_LIMIT,
    BybitLeadStream,
    StreamStats,
    _peek_timestamp,
)
from t3_engine.lead_engine.config import LeadEngineConfig
from t3_engine.lead_engine.engine import LeadEngine
from t3_engine.lead_engine.orderbook_engine import OrderBook


def _stream(on_message=lambda topic, msg: None, symbols=("INJUSDT",)):
    return BybitLeadStream(symbols=list(symbols), kline_intervals=["1"],
                           on_message=on_message, url="wss://example.invalid",
                           depth=50)


# ---- the seam between the socket and the work ---------------------------

def test_a_frame_is_enqueued_not_handled_on_the_receiving_thread():
    """The whole point. `_receive` is what the socket loop calls, and it
    must not reach the handler - otherwise the scoring work and the
    keepalive ping are the same thread again."""
    handled = []
    stream = _stream(on_message=lambda topic, msg: handled.append(topic))
    raw = json.dumps({"topic": "publicTrade.INJUSDT", "ts": 1700000000000,
                      "data": [{"T": 1700000000000, "S": "Buy", "v": "1", "p": "5.7"}]})

    stream._receive(raw, time.time())
    assert handled == [], "the receiving thread did the work"
    assert stream.stats.messages == 1
    assert stream._queue.qsize() == 1

    # ... and the worker does it.
    stream._queue.put_nowait(None)               # stop after one
    stream._drain()
    assert handled == ["publicTrade.INJUSDT"]


def test_arrival_latency_is_sampled_before_the_frame_waits():
    """A backlog on this side must never read as a slow link. The arrival
    stamp is taken when the frame LANDS; whatever happens in the queue
    afterwards is a separate number."""
    stream = _stream()
    exchange_ms = int(time.time() * 1000) - 150
    raw = json.dumps({"topic": "tickers.INJUSDT", "ts": exchange_ms, "data": {}})
    received_at = time.time()

    stream._receive(raw, received_at)
    assert 100 <= stream.stats.network_latency_ms <= 400

    # Two seconds of queue wait changes the queue figure and nothing else.
    stream.stats.note_queue_wait(2_000.0)
    assert stream.stats.queue_latency_ms == pytest.approx(2_000.0)
    assert stream.stats.network_latency_ms < 400


def test_the_timestamp_peek_agrees_with_a_full_parse():
    """Read without parsing, because `json.loads` on a multi-kilobyte
    book delta is exactly the cost the socket thread must not pay."""
    for frame in ({"topic": "orderbook.50.INJUSDT", "ts": 1789491439926, "data": {}},
                  {"ts": 1700000000000, "topic": "publicTrade.INJUSDT"},
                  {"topic": "x", "ts": 1, "data": []}):
        raw = json.dumps(frame)
        assert _peek_timestamp(raw) == frame["ts"]
        assert _peek_timestamp(raw.encode()) == frame["ts"]
    assert _peek_timestamp('{"topic":"x"}') == 0
    assert _peek_timestamp(None) == 0


def test_an_overflowing_queue_drops_the_oldest_and_asks_for_a_resync():
    """A consumer that blocks its producer to stay complete only falls
    further behind. Dropping is the right answer - and a dropped frame
    means the book must be rebuilt, not quietly continued."""
    resyncs = []
    stream = _stream(symbols=("INJUSDT", "BTCUSDT"))
    stream.on_resync(lambda symbols: resyncs.append(list(symbols)))

    raw = json.dumps({"topic": "tickers.INJUSDT", "ts": 1700000000000, "data": {}})
    for _ in range(QUEUE_LIMIT):
        stream._queue.put_nowait((raw, time.time()))
    assert stream._queue.full()

    stream._receive(raw, time.time())
    assert stream.stats.dropped > 0
    assert resyncs and set(resyncs[0]) == {"INJUSDT", "BTCUSDT"}
    assert not stream._queue.full(), "room was made for the newest frame"


def test_the_rate_is_measured_over_what_was_observed():
    """A rate averaged since start hides the minute that matters."""
    stats = StreamStats()
    now = time.time()
    for index in range(100):
        stats.note_arrival(0, now + index * 0.01)   # 100 frames over 0.99s
    assert 95 <= stats.messages_per_second <= 105
    assert stats.as_dict()["messages_per_second"] == pytest.approx(
        stats.messages_per_second, abs=0.1)


# ---- resync ------------------------------------------------------------

def test_a_reconnect_throws_the_local_book_away():
    """A reconnected socket says nothing about the book it left behind,
    and Bybit only sends a snapshot on subscribe."""
    engine = LeadEngine(LeadEngineConfig(enabled=True, symbols=["INJUSDT"]))
    now = int(time.time() * 1000)
    engine.handle_message("orderbook.50.INJUSDT", {
        "topic": "orderbook.50.INJUSDT", "type": "snapshot", "ts": now,
        "data": {"u": 1, "b": [["5.70", "100"]], "a": [["5.71", "100"]]}})
    state = engine.states["INJUSDT"]
    assert state.book.synced is True

    engine._reset_books(["INJUSDT"])
    assert state.book.synced is False
    assert state.book.bids == {} and state.book.asks == {}
    assert state.book.last_update_id is None
    assert state.health.orderbook_synced is False
    assert state.book.resyncs == 1


def test_a_sequence_gap_asks_for_a_new_snapshot_instead_of_dying_quietly():
    """The defect: a book that desynced stayed dead until the next
    reconnect, because nothing asked the exchange for another snapshot."""
    engine = LeadEngine(LeadEngineConfig(enabled=True, symbols=["INJUSDT"]))
    asked = []

    class _FakeStream:
        stats = StreamStats()

        def _demand_resync(self, symbols, reason):
            asked.append((list(symbols), reason))

    engine.stream = _FakeStream()
    now = int(time.time() * 1000)
    engine.handle_message("orderbook.50.INJUSDT", {
        "topic": "orderbook.50.INJUSDT", "type": "snapshot", "ts": now,
        "data": {"u": 1, "b": [["5.70", "100"]], "a": [["5.71", "100"]]}})
    engine.handle_message("orderbook.50.INJUSDT", {
        "topic": "orderbook.50.INJUSDT", "type": "delta", "ts": now + 1,
        "data": {"u": 9, "b": [["5.70", "90"]], "a": []}})   # expected 2

    assert asked, "a gap must ask for a snapshot"
    symbols, reason = asked[0]
    assert symbols == ["INJUSDT"]
    assert "expected 2, got 9" in reason


def test_the_book_reports_the_counters_the_brief_asks_for():
    book = OrderBook("INJUSDT")
    book.apply({"type": "snapshot", "ts": 1,
                "data": {"u": 1, "b": [["5.70", "10"]], "a": [["5.71", "10"]]}})
    book.apply({"type": "delta", "ts": 2,
                "data": {"u": 2, "b": [["5.70", "9"]], "a": []}})
    book.apply({"type": "delta", "ts": 3,
                "data": {"u": 8, "b": [["5.70", "8"]], "a": []}})     # a gap
    stats = book.sequence_stats()
    assert stats["snapshots_received"] == 1
    assert stats["deltas_received"] == 1
    assert stats["sequence_gaps"] == 1
    assert stats["resync_count"] == 0
    assert stats["synced"] is False
    assert "expected 3, got 8" in stats["desync_reason"]

    book.reset("reconnect")
    assert book.sequence_stats()["resync_count"] == 1


# ---- the cost of a delta ------------------------------------------------

def test_the_sorted_book_is_cached_per_version_not_per_question():
    """`metrics()` asks for the top of the book about fifty times.
    Profiled at 141,000 sorts for 3,000 deltas, which is why the consumer
    could not keep up with a 200 frame/second feed."""
    book = OrderBook("INJUSDT", depth=50)
    book.apply({"type": "snapshot", "ts": 1, "data": {
        "u": 1,
        "b": [[f"{5.70 - i * 0.001:.4f}", "100"] for i in range(50)],
        "a": [[f"{5.71 + i * 0.001:.4f}", "100"] for i in range(50)]}})

    sorts = {"count": 0}
    import builtins

    import t3_engine.lead_engine.orderbook_engine as module

    def counting_sorted(*args, **kwargs):
        sorts["count"] += 1
        return builtins.sorted(*args, **kwargs)

    # A module global shadows the builtin for code inside that module.
    module.sorted = counting_sorted
    try:
        book.metrics()
        first = sorts["count"]
        book.metrics()                           # same version: no new sorts
        assert sorts["count"] == first
        assert first <= 4, f"{first} sorts for one metric set"
    finally:
        del module.sorted


def test_the_cache_still_follows_the_book():
    """Cached per VERSION, so a delta must invalidate it. A stale top of
    book is worse than a slow one."""
    book = OrderBook("INJUSDT")
    book.apply({"type": "snapshot", "ts": 1, "data": {
        "u": 1, "b": [["5.70", "10"], ["5.69", "10"]], "a": [["5.71", "10"]]}})
    assert book.top_bids(1) == [(5.70, 10.0)]

    book.apply({"type": "delta", "ts": 2, "data": {
        "u": 2, "b": [["5.705", "7"]], "a": []}})
    assert book.top_bids(1) == [(5.705, 7.0)], "the cache outlived its book"

    book.apply({"type": "delta", "ts": 3, "data": {
        "u": 3, "b": [["5.705", "0"]], "a": []}})
    assert book.top_bids(1) == [(5.70, 10.0)]


def test_side_totals_agree_with_the_full_metric_set():
    """The two depths the pre-break engine wants, without computing the
    forty other numbers it does not."""
    book = OrderBook("INJUSDT")
    book.apply({"type": "snapshot", "ts": 1, "data": {
        "u": 1,
        "b": [[f"{5.70 - i * 0.001:.4f}", str(10 + i)] for i in range(30)],
        "a": [[f"{5.71 + i * 0.001:.4f}", str(20 + i)] for i in range(30)]}})
    metrics = book.metrics()
    bid_depth, ask_depth = book.side_totals()
    assert bid_depth == pytest.approx(metrics.bid_depth)
    assert ask_depth == pytest.approx(metrics.ask_depth)


def test_routing_a_book_delta_costs_far_less_than_the_feed_allows():
    """A budget, not a benchmark. Bybit sends about 200 frames a second
    across seven symbols; anything near a millisecond a frame cannot keep
    up on a shared core, and the backlog that follows is unbounded."""
    engine = LeadEngine(LeadEngineConfig(enabled=True, symbols=["INJUSDT"]))
    now = int(time.time() * 1000)
    engine.handle_message("orderbook.50.INJUSDT", {
        "topic": "orderbook.50.INJUSDT", "type": "snapshot", "ts": now,
        "data": {"u": 1,
                 "b": [[f"{5.70 - i * 0.001:.4f}", "100"] for i in range(50)],
                 "a": [[f"{5.71 + i * 0.001:.4f}", "100"] for i in range(50)]}})

    frames = 600
    began = time.perf_counter()
    for index in range(frames):
        engine.handle_message("orderbook.50.INJUSDT", {
            "topic": "orderbook.50.INJUSDT", "type": "delta", "ts": now + index,
            "data": {"u": index + 2,
                     "b": [[f"{5.70 - j * 0.001:.4f}", str(90 + j)] for j in range(10)],
                     "a": [[f"{5.71 + j * 0.001:.4f}", str(90 + j)] for j in range(10)]}})
    per_frame_ms = (time.perf_counter() - began) / frames * 1000.0
    # Generous: it measures at about 0.05ms here, and CI is slower than a
    # laptop. The number that matters is that it is nowhere near 1ms.
    assert per_frame_ms < 0.5, f"{per_frame_ms:.3f}ms per book frame"
