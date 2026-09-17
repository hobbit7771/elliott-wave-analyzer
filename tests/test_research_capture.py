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
    assert [f["topic"] for f in recorder._buffer] == [
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
        recorder.observe("orderbook.50.INJUSDT", _frame(ts=i), i)

    assert recorder.flush(force=True) is None
    assert written == []
    assert recorder.stats.state == "EXHAUSTED"
    assert recorder.stats.stored_bytes == 0


def test_a_segment_that_fits_is_written_and_counted():
    written = []
    recorder = _recorder(budget_mb=10.0, writer=written.append)
    for i in range(50):
        recorder.observe("orderbook.50.INJUSDT", _frame(ts=i), i)
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
