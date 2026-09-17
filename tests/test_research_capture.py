"""The recorder that produces the only real data this project will have.

Its failure modes are all quiet ones - a budget passed, a frame dropped
without a record, a payload that no longer matches its hash - so each of
them gets a test that would notice.
"""

import json

import pytest

from t3_engine.research import capture as cap


def _frame(topic="orderbook.50.INJUSDT", ts=1_700_000_000_000, kind="delta",
           data=None):
    return {"ts": ts, "type": kind, "data": data or {"u": 1, "b": [["5.7", "10"]], "a": []}}


def _recorder(**kwargs):
    kwargs.setdefault("symbols", ["INJUSDT"])
    kwargs.setdefault("writer", lambda rows: None)
    kwargs.setdefault("session_id", "test-session")
    recorder = cap.CaptureRecorder(**kwargs)
    recorder.stats.state = "RECORDING"          # without starting the thread
    return recorder


# ---- the tap ------------------------------------------------------------

def test_only_the_recorded_topics_and_symbols_are_kept():
    recorder = _recorder()
    recorder.observe("orderbook.50.INJUSDT", _frame(), 1)
    recorder.observe("publicTrade.INJUSDT", _frame(), 2)
    recorder.observe("kline.1.INJUSDT", _frame(), 3)        # not recorded: derivable
    recorder.observe("orderbook.50.BTCUSDT", _frame(), 4)   # not our symbol
    assert recorder.stats.frames_seen == 2
    recorder.flush(force=True)                 # closes the merge window
    assert sorted(f["topic"] for f in recorder._last_frames) == [
        "orderbook.50.INJUSDT", "publicTrade.INJUSDT"]


def test_a_stopped_recorder_ignores_frames_rather_than_buffering_them():
    recorder = _recorder()
    recorder.stats.state = "EXHAUSTED"
    recorder.observe("publicTrade.INJUSDT", _frame(), 1)
    assert recorder.stats.frames_seen == 0
    assert not recorder._buffer


def test_an_overflowing_buffer_drops_the_oldest_and_counts_it():
    """Silence is the failure mode that matters: a gap has to be visible
    in the data, not inferred from its absence."""
    recorder = _recorder(buffer_limit=3)
    for i in range(5):
        recorder.observe("publicTrade.INJUSDT", _frame(ts=i), i)
    assert len(recorder._buffer) == 3
    assert [f["ts"] for f in recorder._buffer] == [2, 3, 4]
    assert recorder.stats.frames_dropped == 2

    written = []
    recorder._writer = written.append
    segment = recorder.flush(force=True)
    assert segment.dropped_before == 2
    assert segment.to_row()["dropped_before"] == 2


# ---- the payload --------------------------------------------------------

def test_a_segment_round_trips_to_exactly_the_frames_that_went_in():
    recorder = _recorder()
    sent = []
    for i in range(20):
        frame = _frame(ts=1_700_000_000_000 + i * 100)
        sent.append(frame)
        recorder.observe("orderbook.50.INJUSDT", frame, 1_700_000_000_050 + i * 100)
    segment = recorder.flush(force=True)

    back = cap.decode_segment(segment.payload)
    assert len(back) == 20
    assert [f["ts"] for f in back] == [f["ts"] for f in sent]
    assert back[0]["data"] == sent[0]["data"]
    # Both clocks survive, which is what makes causality checkable later.
    assert back[0]["r"] == 1_700_000_000_050
    assert segment.first_exchange_ms == 1_700_000_000_000
    assert segment.last_exchange_ms == 1_700_000_001_900


def test_verify_segment_accepts_an_intact_row_and_rejects_a_tampered_one():
    recorder = _recorder()
    for i in range(5):
        recorder.observe("publicTrade.INJUSDT", _frame(ts=i), i)
    row = recorder.flush(force=True).to_row()

    ok, why = cap.verify_segment(row)
    assert ok and why == ""

    row["sha256"] = "0" * 64
    ok, why = cap.verify_segment(row)
    assert not ok and "sha256" in why


def test_the_hash_covers_the_raw_frames_not_the_base64():
    """So it still means something to someone holding the decompressed
    file, who never sees our encoding."""
    import base64, gzip, hashlib

    recorder = _recorder()
    recorder.observe("publicTrade.INJUSDT", _frame(), 1)
    segment = recorder.flush(force=True)
    raw = gzip.decompress(base64.b64decode(segment.payload))
    assert hashlib.sha256(raw).hexdigest() == segment.sha256


# ---- the budget ---------------------------------------------------------

def test_the_budget_is_checked_before_the_write_not_after():
    """Discovering a quota by filling it destroys the thing it fed."""
    written = []
    recorder = _recorder(budget_mb=0.0005, writer=written.append)   # ~500 bytes
    for i in range(400):
        recorder.observe("publicTrade.INJUSDT", _frame(ts=i), i)

    assert recorder.flush(force=True) is None
    assert written == []
    assert recorder.stats.state == "EXHAUSTED"
    assert recorder.stats.stored_bytes == 0


def test_a_segment_that_fits_is_written_and_counted():
    written = []
    recorder = _recorder(budget_mb=10.0, writer=written.append)
    for i in range(50):
        recorder.observe("publicTrade.INJUSDT", _frame(ts=i), i)
    segment = recorder.flush(force=True)

    assert len(written) == 1
    assert recorder.stats.segments_written == 1
    assert recorder.stats.frames_written == 50
    assert recorder.stats.stored_bytes == segment.stored_bytes
    assert recorder.stats.raw_bytes > recorder.stats.stored_bytes   # it compressed


def test_segment_ids_are_stable_and_ordered_so_a_retry_upserts():
    recorder = _recorder()
    ids = []
    for _ in range(3):
        recorder.observe("publicTrade.INJUSDT", _frame(), 1)
        ids.append(recorder.flush(force=True).segment_id)
    assert ids == ["test-session:000001", "test-session:000002", "test-session:000003"]
    assert len(set(ids)) == 3


# ---- flush triggers -----------------------------------------------------

def test_flush_happens_on_the_frame_count():
    recorder = _recorder(frames_per_segment=10, seconds_per_segment=10_000.0)
    for i in range(9):
        recorder.observe("publicTrade.INJUSDT", _frame(ts=i), i)
    assert recorder.flush() is None
    recorder.observe("publicTrade.INJUSDT", _frame(ts=9), 9)
    assert recorder.flush() is not None


def test_flush_happens_on_the_clock_even_when_the_market_is_quiet():
    now = [1_000.0]
    recorder = _recorder(frames_per_segment=10_000, seconds_per_segment=60.0,
                         clock=lambda: now[0])
    recorder.observe("publicTrade.INJUSDT", _frame(), 1)
    assert recorder.flush() is None
    now[0] += 61.0
    segment = recorder.flush()
    assert segment is not None and segment.frames == 1


def test_an_empty_buffer_never_writes_a_row():
    written = []
    recorder = _recorder(writer=written.append)
    assert recorder.flush(force=True) is None
    assert written == []


def test_a_write_failure_raises_and_does_not_count_the_bytes():
    """A failed write that still charged the budget would end recording
    early for a segment nobody has."""
    def boom(rows):
        raise RuntimeError("supabase said no")

    recorder = _recorder(writer=boom)
    recorder.observe("publicTrade.INJUSDT", _frame(), 1)
    with pytest.raises(RuntimeError):
        recorder.flush(force=True)
    assert recorder.stats.stored_bytes == 0
    assert recorder.stats.segments_written == 0
    assert recorder.stats.write_failures == 1
    assert "supabase said no" in recorder.stats.last_error


# ---- configuration ------------------------------------------------------

def test_the_recorder_does_not_start_itself_without_the_environment(monkeypatch):
    monkeypatch.delenv(cap.ENABLED_ENV, raising=False)
    monkeypatch.delenv(cap.ENABLED_ENV_PREFIXED, raising=False)
    cap.reset_recorder()
    assert cap.start_recorder(["INJUSDT"]) is None
    assert cap.get_recorder() is None


def test_it_only_records_a_symbol_that_is_actually_subscribed(monkeypatch):
    monkeypatch.setenv(cap.ENABLED_ENV, "true")
    monkeypatch.setenv(cap.SYMBOLS_ENV, "SOMETHINGUSDT")
    cap.reset_recorder()
    assert cap.start_recorder(["INJUSDT"]) is None
    cap.reset_recorder()


def test_the_real_writer_calls_supabase_with_a_signature_that_exists():
    """The unit tests above all pass a fake writer, so the REAL writer's
    call was never exercised - and it shipped with a keyword argument
    `insert()` does not take. Every flush failed in production with
    "unexpected keyword argument 'upsert'" until this test existed.

    Bind the call against the actual function rather than mocking the
    module, so a rename on either side fails here rather than in a log."""
    import inspect

    from t3_engine.database import supabase_rest

    captured = {}

    def fake_insert(table, rows, client=None, on_conflict=None):
        captured.update(table=table, rows=rows, on_conflict=on_conflict)
        return []

    # The fake must be substitutable for the real one, or this test
    # proves nothing about the real one.
    real = inspect.signature(supabase_rest.insert)
    assert set(real.parameters) == set(inspect.signature(fake_insert).parameters)

    original_insert = supabase_rest.insert
    original_configured = supabase_rest.configured
    supabase_rest.insert = fake_insert
    supabase_rest.configured = lambda: True
    try:
        cap._supabase_writer([{"segment_id": "s:1", "payload": "x"}])
    finally:
        supabase_rest.insert = original_insert
        supabase_rest.configured = original_configured

    assert captured["table"] == cap.TABLE_CAPTURES
    assert captured["on_conflict"] == "segment_id"


def test_an_unconfigured_supabase_refuses_rather_than_discarding_frames():
    from t3_engine.database import supabase_rest

    original = supabase_rest.configured
    supabase_rest.configured = lambda: False
    try:
        with pytest.raises(RuntimeError, match="refusing to discard"):
            cap._supabase_writer([{"segment_id": "s:1"}])
    finally:
        supabase_rest.configured = original


# ---- coalescing ---------------------------------------------------------

def _delta(recv_ms, bids=(), asks=(), u=1, ts=None):
    return ("orderbook.50.INJUSDT",
            {"ts": ts if ts is not None else recv_ms, "type": "delta",
             "data": {"u": u, "b": [list(x) for x in bids],
                      "a": [list(x) for x in asks]}},
            recv_ms)


def test_merging_deltas_reconstructs_the_book_exactly_at_each_boundary():
    """The claim coalescing rests on. A delta states the size a price NOW
    holds, so the last word inside a window is the whole truth about that
    window's end - and replaying merged windows must give byte-identical
    books to replaying every frame."""
    from t3_engine.research.book import BookReconstructor

    raw = [
        ("orderbook.50.INJUSDT",
         {"ts": 1000, "type": "snapshot",
          "data": {"u": 1, "b": [["5.75", "100"], ["5.74", "200"]],
                   "a": [["5.76", "80"], ["5.77", "150"]]}}, 1000),
        _delta(1010, bids=[("5.75", "90")], u=2),
        _delta(1050, bids=[("5.75", "70")], asks=[("5.76", "60")], u=3),
        _delta(1099, bids=[("5.74", "0")], u=4),          # last of window 10
        _delta(1105, asks=[("5.76", "10")], u=5),         # window 11
        _delta(1190, bids=[("5.73", "300")], u=6),
    ]

    recorder2 = _recorder()
    for topic, message, recv in raw:
        recorder2.observe(topic, message, recv)
    segment = recorder2.flush(force=True)
    merged = cap.decode_segment(segment.payload)

    full = BookReconstructor()
    coalesced = BookReconstructor()
    for topic, message, recv in raw:
        full.apply({"type": message["type"], "ts": message["ts"], "r": recv,
                    "data": message["data"]})
    for frame in merged:
        coalesced.apply(frame)

    a, b = full.state(), coalesced.state()
    assert a.bids == b.bids and a.asks == b.asks
    assert b.bids == [(5.75, 70.0), (5.73, 300.0)]
    assert b.asks == [(5.76, 10.0), (5.77, 150.0)]
    # And it really did compress: six frames in, fewer out.
    assert len(merged) < len(raw)
    assert recorder2.stats.frames_coalesced > 0


def test_a_snapshot_is_never_merged_into_a_delta():
    """A snapshot is a new starting point. Folding it into a delta would
    turn "the book is exactly this" into "these levels changed"."""
    recorder = _recorder()
    recorder.observe(*_delta(1010, bids=[("5.75", "90")]))
    recorder.observe("orderbook.50.INJUSDT",
                     {"ts": 1020, "type": "snapshot",
                      "data": {"u": 9, "b": [["5.70", "1"]], "a": [["5.71", "1"]]}},
                     1020)
    segment = recorder.flush(force=True)
    frames = cap.decode_segment(segment.payload)
    assert [f["type"] for f in frames] == ["delta", "snapshot"]
    assert frames[1]["data"]["b"] == [["5.70", "1"]]


def test_trades_and_liquidations_are_never_coalesced():
    """They are the information-bearing events: the tape is what fills a
    maker and what shows aggression."""
    recorder = _recorder()
    for i in range(10):
        recorder.observe("publicTrade.INJUSDT",
                         {"ts": 1000 + i, "type": "snapshot",
                          "data": [{"p": "5.75", "v": "1", "S": "Buy"}]}, 1000 + i)
    segment = recorder.flush(force=True)
    assert segment.frames == 10
    assert recorder.stats.frames_coalesced == 0


def test_tickers_are_thinned_to_one_every_few_seconds():
    """31% of frames and over half the bytes, for a 1Hz republication of
    a funding rate that changes every eight hours."""
    recorder = _recorder()
    for i in range(20):
        recorder.observe("tickers.INJUSDT",
                         {"ts": 1000 + i * 1000, "type": "snapshot",
                          "data": {"fundingRate": "0.0001"}}, 1000 + i * 1000)
    segment = recorder.flush(force=True)
    assert segment.frames == 4                 # 20 seconds at one per five
    assert recorder.stats.frames_coalesced == 16


def test_a_flush_never_ends_in_the_middle_of_a_merge_window():
    """An unflushed pending delta at the end of a segment would be lost
    at shutdown - a silent hole at every restart."""
    recorder = _recorder()
    recorder.observe(*_delta(1010, bids=[("5.75", "90")]))
    segment = recorder.flush(force=True)
    assert segment is not None and segment.frames == 1
    assert cap.decode_segment(segment.payload)[0]["data"]["b"] == [["5.75", "90"]]


# ---- the seeded snapshot ------------------------------------------------

def test_a_capture_without_a_snapshot_cannot_be_replayed_at_all():
    """The defect this exists to fix, stated as a test. Bybit sends a
    snapshot only on SUBSCRIBE; the recorder is installed after that, so
    it joins a stream of deltas with nothing to apply them to."""
    from t3_engine.research.book import BookReconstructor

    book = BookReconstructor()
    for i in range(50):
        state = book.apply({"type": "delta", "ts": i, "r": i,
                            "data": {"u": i, "b": [["5.75", "10"]], "a": []}})
        assert state is None
    assert book.synced is False


def test_a_seeded_snapshot_makes_the_following_deltas_replayable():
    from t3_engine.research.book import BookReconstructor

    recorder = _recorder()
    assert recorder.seed_snapshot("INJUSDT",
                                  {5.75: 100.0, 5.74: 200.0},
                                  {5.76: 80.0, 5.77: 150.0}, at_ms=1_000)
    recorder.observe(*_delta(1_100, bids=[("5.75", "90")], u=7))
    frames = cap.decode_segment(recorder.flush(force=True).payload)

    assert frames[0]["type"] == "snapshot" and frames[0]["synthetic"] is True
    book = BookReconstructor()
    for frame in frames:
        book.apply(frame)
    state = book.state()
    assert state.best_bid == 5.75 and state.best_ask == 5.76
    assert state.size_at("bid", 5.75) == 90.0        # the delta applied
    assert recorder.stats.seeded_snapshots == 1


def test_a_seed_is_marked_synthetic_so_the_manifest_cannot_mistake_it():
    recorder = _recorder()
    recorder.seed_snapshot("INJUSDT", {5.75: 1.0}, {5.76: 1.0}, at_ms=1)
    frame = cap.decode_segment(recorder.flush(force=True).payload)[0]
    assert frame["synthetic"] is True
    assert frame["data"]["u"] == 0


def test_an_unsynced_or_empty_book_is_not_seeded_half_formed():
    recorder = _recorder()
    assert recorder.seed_snapshot("INJUSDT", {}, {5.76: 1.0}) is False
    assert recorder.seed_snapshot("INJUSDT", {5.75: 1.0}, {}) is False
    assert recorder.seed_snapshot("OTHERUSDT", {5.75: 1.0}, {5.76: 1.0}) is False
    assert recorder.stats.seeded_snapshots == 0


def test_seed_from_engine_skips_a_symbol_whose_book_is_not_synced():
    class FakeBook:
        def __init__(self, synced):
            self.synced = synced
            self.bids = {5.75: 10.0}
            self.asks = {5.76: 10.0}

    class FakeEngine:
        states = {"INJUSDT": type("S", (), {"book": FakeBook(False)})()}

    recorder = _recorder()
    assert cap.seed_from_engine(recorder, FakeEngine()) == 0

    FakeEngine.states["INJUSDT"].book.synced = True
    assert cap.seed_from_engine(recorder, FakeEngine()) == 1
