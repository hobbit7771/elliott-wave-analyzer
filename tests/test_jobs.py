"""The background-job layer.

These exist because of a measured production failure, not a hypothetical
one: a multi-timeframe run took 12m13s on the real deploy, the server
answered correctly, and the page showed "Load failed" because the phone
had dropped that connection minutes earlier - after the tokens had been
spent. The job layer is what makes the run outlive the request that asked
for it.
"""

import time

import pytest

from t3_engine.ai_advisor import jobs


@pytest.fixture(autouse=True)
def clean_registry():
    jobs.clear_all()
    yield
    jobs.clear_all()


def _wait_for(job, status, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if job.status == status:
            return True
        time.sleep(0.01)
    return False


def test_a_job_returns_immediately_and_finishes_in_the_background():
    released = []

    def work(note):
        note("thinking")
        released.append(1)
        return {"answer": 42}

    started = time.monotonic()
    job = jobs.start("analyst", "synthetic:X:5m", work)
    # The POST must return in milliseconds - that IS the fix.
    assert time.monotonic() - started < 0.5
    assert _wait_for(job, "done")
    assert job.result == {"answer": 42}
    assert [line["text"] for line in job.progress] == ["thinking"]


def test_a_failing_job_keeps_the_reason_instead_of_vanishing():
    def work(note):
        note("about to fail")
        raise RuntimeError("the model said no")

    job = jobs.start("analyst", "synthetic:X:5m", work)
    assert _wait_for(job, "error")
    assert "the model said no" in job.error
    assert job.result is None
    # the failure is in the log too - a run that died silently is
    # indistinguishable from one still working
    assert any("Failed" in line["text"] for line in job.progress)


def test_an_identical_request_finds_the_running_job_rather_than_paying_twice():
    """Two taps, two tabs, or a retry after a dropped connection. Starting
    a second identical run is how the same four analyst runs get bought
    twice."""
    gate = {"open": False}

    def work(note):
        while not gate["open"]:
            time.sleep(0.01)
        return {"ok": True}

    job = jobs.start("multi", "bybit:INJUSDT:mtf", work)
    assert jobs.find_running("multi", "bybit:INJUSDT:mtf") is job
    # a different instrument, or a different kind of run, is different work
    assert jobs.find_running("multi", "bybit:BTCUSDT:mtf") is None
    assert jobs.find_running("analyst", "bybit:INJUSDT:mtf") is None
    gate["open"] = True
    assert _wait_for(job, "done")
    # and once it is finished there is nothing to join - the next request
    # is a new run against newer candles
    assert jobs.find_running("multi", "bybit:INJUSDT:mtf") is None


def test_progress_is_capped_so_a_long_run_does_not_grow_without_bound():
    job = jobs.Job(job_id="x", kind="analyst", label="l")
    for i in range(jobs.MAX_PROGRESS_LINES + 50):
        job.note(f"line {i}")
    assert len(job.progress) == jobs.MAX_PROGRESS_LINES
    # the TAIL is what says where a run is, so that is what survives
    assert job.progress[-1]["text"] == f"line {jobs.MAX_PROGRESS_LINES + 49}"


def test_finished_jobs_are_pruned_but_a_running_one_is_never_dropped():
    done = jobs.start("analyst", "a", lambda note: {"ok": True})
    assert _wait_for(done, "done")
    done.updated_at = time.time() - jobs.FINISHED_TTL_SECONDS - 10

    gate = {"open": False}

    def slow(note):
        while not gate["open"]:
            time.sleep(0.01)
        return {"ok": True}

    running = jobs.start("analyst", "b", slow)
    jobs.start("analyst", "c", lambda note: {"ok": True})     # triggers the prune

    assert jobs.get(done.job_id) is None
    assert jobs.get(running.job_id) is not None
    gate["open"] = True


def test_snapshot_can_withhold_the_result_for_a_progress_only_poll():
    job = jobs.Job(job_id="x", kind="analyst", label="l", result={"big": "payload"})
    assert job.snapshot()["result"] == {"big": "payload"}
    assert job.snapshot(include_result=False)["result"] is None
    assert job.snapshot()["elapsed_seconds"] >= 0
