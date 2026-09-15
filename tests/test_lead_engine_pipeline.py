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
import math
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


# ---- the forming bar comes from the exchange ----------------------------

def test_the_forming_bar_is_aggregated_from_bybits_own_klines():
    """The chart used to build this from a price polled every 500ms and
    the browser's clock, which loses every high and low between two polls
    and reports a volume of zero. Bybit sends the real thing on kline.1."""
    from t3_engine.lead_engine.config import LeadEngineConfig
    from t3_engine.lead_engine.engine import LeadEngine

    engine = LeadEngine(LeadEngineConfig(enabled=True, symbols=["INJUSDT"]))
    now = int(time.time() * 1000)
    bar = (now // 300_000) * 300_000          # the current five-minute bar

    minutes = [
        {"start": bar, "open": "5.70", "high": "5.72", "low": "5.69",
         "close": "5.71", "volume": "100", "confirm": True},
        # A wick a 500ms poll would never see, and the volume behind it.
        {"start": bar + 60_000, "open": "5.71", "high": "5.88", "low": "5.60",
         "close": "5.75", "volume": "250", "confirm": True},
        {"start": bar + 120_000, "open": "5.75", "high": "5.77", "low": "5.74",
         "close": "5.76", "volume": "40", "confirm": False},
    ]
    for minute in minutes:
        engine.handle_message("kline.1.INJUSDT", {
            "topic": "kline.1.INJUSDT", "ts": now, "data": [minute]})

    candle = engine.states["INJUSDT"].live_candle(300, now_ms=bar + 150_000)
    assert candle is not None
    assert candle["open"] == 5.70 and candle["close"] == 5.76
    assert candle["high"] == 5.88, "the spike must survive"
    assert candle["low"] == 5.60, "so must the low"
    assert candle["volume"] == 390.0, "volume is the exchange's, summed"
    assert candle["closed"] is False
    assert candle["source"] == "bybit_kline_1m"
    assert candle["minutes_used"] == 3
    assert candle["minutes_confirmed"] == 2
    assert candle["last_minute_confirmed"] is False


def test_a_coarser_timeframe_is_an_exact_sum_of_its_minutes():
    """Aggregating from kline.1 rather than subscribing to every chart
    timeframe is what keeps the topic count at 35. It is only correct
    because a coarser bar IS the sum of its minutes."""
    from t3_engine.lead_engine.config import LeadEngineConfig
    from t3_engine.lead_engine.engine import LeadEngine

    engine = LeadEngine(LeadEngineConfig(enabled=True, symbols=["INJUSDT"]))
    now = int(time.time() * 1000)
    bar = (now // 3_600_000) * 3_600_000      # the current hour
    highs, lows, volumes = [], [], []
    for index in range(12):
        high = 5.70 + index * 0.01
        low = 5.60 - index * 0.005
        highs.append(high)
        lows.append(low)
        volumes.append(10.0 + index)
        engine.handle_message("kline.1.INJUSDT", {
            "topic": "kline.1.INJUSDT", "ts": now, "data": [{
                "start": bar + index * 60_000, "open": "5.65",
                "high": str(high), "low": str(low), "close": "5.66",
                "volume": str(10.0 + index), "confirm": index < 11}]})

    candle = engine.states["INJUSDT"].live_candle(3600, now_ms=bar + 12 * 60_000)
    assert candle["high"] == max(highs)
    assert candle["low"] == min(lows)
    assert candle["volume"] == pytest.approx(sum(volumes))
    assert candle["minutes_used"] == 12


def test_no_kline_data_yields_nothing_rather_than_a_guess():
    from t3_engine.lead_engine.config import LeadEngineConfig
    from t3_engine.lead_engine.engine import LeadEngine

    engine = LeadEngine(LeadEngineConfig(enabled=True, symbols=["INJUSDT"]))
    engine._ensure("INJUSDT")
    assert engine.states["INJUSDT"].live_candle(300) is None


# ---- a read must not change the answer ---------------------------------

def _zigzag(count, base, slope, amplitude, start=0):
    """A clean alternating series, so fractal swings of BOTH kinds
    confirm. A monotone staircase produces highs and no lows."""
    import math

    from t3_engine.lead_engine.smc_engine import Candle

    out = []
    for index in range(count):
        price = base + slope * index + amplitude * math.sin(index * math.pi / 3.0)
        out.append(Candle(start_ms=(start + index) * 60_000, open=price,
                          high=price * 1.0005, low=price * 0.9995,
                          close=price, volume=10.0, closed=True))
    return out


def test_reading_the_structure_twice_gives_the_same_answer():
    """CHoCH is the most consequential label this engine produces - it is
    what `pressure_component` weighs most heavily - and it depended on
    CALL ORDER.

    `state()` advanced `self._trend` as a side effect, and it is called
    several times per snapshot: by the structure layer, by the frame
    assembler, by the multi-timeframe block. The first caller saw a break
    against the prevailing trend and got CHoCH, which committed the new
    trend; every caller after it saw the same break WITH the now-current
    trend and got BOS. Same bar, same data, different answer.

    Measured on the old code: read 1 CHoCH, reads 2-4 BOS."""
    from t3_engine.lead_engine.smc_engine import SmcEngine

    engine = SmcEngine("INJUSDT", "1")
    down = _zigzag(24, 12.0, -0.12, 0.45)
    for candle in down:
        engine.update(candle)
    established = engine.state()
    assert established.trend == "bearish"
    assert established.swing_high is not None

    # Close decisively above the last swing high: a change of character.
    high = established.swing_high
    for candle in _zigzag(1, high * 1.01, 0.0, 0.0, start=len(down)):
        engine.update(candle)
    for offset, price in enumerate((high * 1.03, high * 1.06)):
        for candle in _zigzag(1, price, 0.0, 0.0, start=len(down) + 1 + offset):
            engine.update(candle)

    reads = [engine.state() for _ in range(4)]
    assert reads[0].choch is True and reads[0].choch_direction == "bullish"
    assert reads[0].bos is False
    signatures = {(r.bos, r.choch, r.bos_direction, r.choch_direction, r.trend)
                  for r in reads}
    assert len(signatures) == 1, f"reading twice changed the answer: {signatures}"


def test_the_structure_still_moves_when_a_bar_actually_closes():
    """Memoised, not frozen. A cache that never invalidates is a worse
    bug than the one it replaced."""
    from t3_engine.lead_engine.smc_engine import SmcEngine

    engine = SmcEngine("INJUSDT", "1")
    for candle in _zigzag(24, 12.0, -0.12, 0.45):
        engine.update(candle)
    before = engine.state()

    for index, candle in enumerate(_zigzag(6, 20.0, 0.5, 0.2, start=24)):
        engine.update(candle)
    after = engine.state()
    assert after is not before
    assert (after.swing_high, after.trend) != (before.swing_high, before.trend)


# ---- no direction without the confidence to name one -------------------

def test_an_exhausted_flush_cannot_name_a_side_on_thin_data():
    """REVERSAL_CANDIDATE returns a direction, and it sat ABOVE the
    confidence and conflict gates - so the one state most likely to fire
    on thin, fast-moving data was the one state that ignored how little
    of the engine had answered."""
    from t3_engine.lead_engine.signal_machine import (
        MIN_DIRECTIONAL_CONFIDENCE,
        SignalInputs,
        SignalMachine,
    )

    def run(confidence, conflict_level="CONFLICT_LOW"):
        machine = SignalMachine("INJUSDT")
        return machine.update(SignalInputs(
            long_pressure=55.0, short_pressure=5.0, conflict=5.0,
            conflict_level=conflict_level, confidence=confidence,
            prebreak_long=0.0, prebreak_short=0.0,
            long_level=5.7, short_level=5.6,
            liquidation_state="EXHAUSTION", healthy=True))

    thin = run(MIN_DIRECTIONAL_CONFIDENCE - 0.01)
    assert thin.state == "WATCH"
    assert "confidence" in thin.reason

    conflicted = run(0.9, conflict_level="CONFLICT_HIGH")
    assert conflicted.state == "WATCH"
    assert "disagree" in conflicted.reason

    confident = run(0.9)
    assert confident.state == "REVERSAL_CANDIDATE"
    assert confident.direction == "long"


def test_no_state_that_names_a_side_escapes_the_gate():
    """Structural rather than case-by-case: whatever the inputs, a state
    carrying a direction may not come out while the gate is closed."""
    from t3_engine.lead_engine.signal_machine import SignalInputs, SignalMachine

    directional = {"PRE_BREAK_LONG", "PRE_BREAK_SHORT", "HIGH_PROBABILITY",
                   "A_PLUS", "REVERSAL_CANDIDATE", "PRE_SIGNAL"}
    for liquidation in ("NEUTRAL", "EXHAUSTION", "CASCADE", "LONG_FLUSH"):
        for probability in (0.0, 65.0, 90.0):
            for pressure in (10.0, 60.0, 95.0):
                machine = SignalMachine("INJUSDT")
                result = machine.update(SignalInputs(
                    long_pressure=pressure, short_pressure=2.0, conflict=1.0,
                    conflict_level="CONFLICT_LOW", confidence=0.05,
                    prebreak_long=probability, prebreak_short=0.0,
                    long_level=5.7, short_level=5.6,
                    liquidation_state=liquidation, healthy=True))
                assert result.state not in directional, (
                    f"{result.state} named a side on confidence 0.05 "
                    f"({liquidation}, p={probability}, pressure={pressure})")


# ---- a quiet market is not a broken feed --------------------------------

def _fresh_health(now_ms, book_age, trade_age):
    from t3_engine.lead_engine.health import StreamHealth

    health = StreamHealth("INJUSDT", ws_connected=True, orderbook_synced=True)
    health.last_book_ms = health.last_book_receive_ms = now_ms - book_age
    health.last_trade_ms = health.last_trade_receive_ms = now_ms - trade_age
    health.last_ticker_ms = now_ms - 500
    return health


def test_a_quiet_tape_with_a_live_book_is_not_a_data_failure():
    """Measured over thirty minutes on the live deployment: between one
    and six of seven symbols sat in DATA_FAILURE continuously while the
    socket was connected, had never reconnected, had zero sequence gaps
    and a book a hundred milliseconds old.

    Nothing was wrong. ATOMUSDT and FILUSDT go more than a second and a
    half without a print, which is ordinary for them. Muting on trade age
    alone reported ordinary behaviour as a fault.

    A silent tape still reaches the signal machine honestly - the flow
    layer has fewer inputs, so its confidence falls, and the confidence
    floor stops a side being named. That is the right mechanism; a health
    verdict is the wrong one."""
    from t3_engine.lead_engine.config import Thresholds
    from t3_engine.lead_engine.health import OK, assess

    now_ms = int(time.time() * 1000)
    thresholds = Thresholds()

    for gap in (4_000, 20_000, 45_000):
        verdict = assess(_fresh_health(now_ms, 100, gap), thresholds, now_ms=now_ms)
        assert verdict.status == OK, f"a {gap}ms trade gap was called {verdict.status}"
        assert verdict.signals_enabled is True
        assert any("quiet tape" in note for note in verdict.reasons)


def test_a_trade_gap_counts_against_the_feed_when_the_book_is_stale_too():
    """Quiet is a property of the market; stale is a property of the
    feed. Only the second is a fault, and the book is what tells them
    apart."""
    from t3_engine.lead_engine.config import Thresholds
    from t3_engine.lead_engine.health import assess

    now_ms = int(time.time() * 1000)
    verdict = assess(_fresh_health(now_ms, 3_000, 4_000), Thresholds(), now_ms=now_ms)
    assert verdict.signals_enabled is False
    assert any("book is stale too" in reason for reason in verdict.reasons)


def test_a_trade_feed_that_died_is_still_caught():
    """An instrument can be quiet. It cannot be silent for five minutes
    while its order book keeps ticking - that is a subscription that
    died, and without a bound the quiet-tape allowance would hide it."""
    from t3_engine.lead_engine.config import Thresholds
    from t3_engine.lead_engine.health import assess

    now_ms = int(time.time() * 1000)
    verdict = assess(_fresh_health(now_ms, 100, 360_000), Thresholds(), now_ms=now_ms)
    assert verdict.signals_enabled is False
    assert any("looks dead" in reason for reason in verdict.reasons)


# ---- the levels are not rebuilt on every look --------------------------

def test_levels_are_rebuilt_once_per_closed_bar_not_once_per_direction():
    """`evaluate` is called once per DIRECTION, so `rebuild` ran twice per
    snapshot, each time walking find_swings and _measure_bounces over the
    whole series. Raising retention to 1,500 bars for item 8 made every
    pass about four times more expensive, and it showed in production:
    median book age drifted 85ms -> 189ms over sixteen minutes as the
    buffers filled, p95 reached 3.6s. Profiled at 63% of a snapshot."""
    from t3_engine.lead_engine.prebreak_engine import LevelTracker
    from t3_engine.lead_engine.smc_engine import Candle

    tracker = LevelTracker()
    bars = []
    for index in range(200):
        price = 5.70 + 0.4 * math.sin(index / 9.0)
        bars.append(Candle(start_ms=index * 15_000, open=price, high=price * 1.001,
                           low=price * 0.999, close=price, volume=10.0, closed=True))

    calls = {"count": 0}
    import t3_engine.lead_engine.prebreak_engine as module
    real = module.find_swings

    def counting(candles, *args, **kwargs):
        calls["count"] += 1
        return real(candles, *args, **kwargs)

    module.find_swings = counting
    try:
        tracker.rebuild(bars)
        first = calls["count"]
        assert first == 1
        for _ in range(10):
            tracker.rebuild(bars)            # same closed series
        assert calls["count"] == first, "rebuilt without a new closed bar"

        levels_before = list(tracker.levels)
        bars.append(Candle(start_ms=200 * 15_000, open=6.4, high=6.5, low=6.3,
                           close=6.45, volume=10.0, closed=True))
        tracker.rebuild(bars)
        assert calls["count"] == first + 1, "a closed bar must rebuild"
        assert tracker.levels is not levels_before
    finally:
        module.find_swings = real


def test_the_first_sync_is_not_counted_as_a_resync():
    """`resyncs` answers "how often did a GOOD book have to be thrown
    away". It cannot answer that if it starts at one per symbol.

    The engine resets every book before subscribing - correct, and a
    no-op on an empty book - and counting those made a clean
    seventeen-minute live run report `resyncs=7` on seven symbols before
    a single frame had arrived, which is indistinguishable from seven
    real desyncs.
    """
    from t3_engine.lead_engine.orderbook_engine import OrderBook

    book = OrderBook("INJUSDT", 50)
    book.reset("first connect")
    book.reset("resubscribe")
    assert book.resyncs == 0, "an empty book has nothing to resync"
    assert book.sequence_stats()["resync_count"] == 0

    now = int(time.time() * 1000)
    book.apply({"type": "snapshot", "ts": now,
                "data": {"u": 1, "b": [["5.70", "10"]], "a": [["5.71", "10"]]}})
    assert book.synced is True
    assert book.resyncs == 0, "syncing is not resyncing"

    # Now there IS a book to lose, so losing it counts.
    book.reset("frames dropped")
    assert book.resyncs == 1
    assert book.sequence_stats()["desync_reason"] == "frames dropped"
