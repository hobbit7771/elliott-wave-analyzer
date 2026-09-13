"""Analyses that outlive the HTTP request which asked for them.

Why this exists, precisely: a multi-timeframe run counts 5m, 15m, 1h and
4h and then reconciles them - four full analyst runs plus a synthesis, a
dozen model calls each. On a real deploy one such run was measured at
**12 minutes 13 seconds** (POST /api/ai/multi, 15:35:56 -> 15:48:09, in
the service's own request log). The server produced a complete, correct
answer. Nobody ever saw it: the browser, the phone's radio and the
hosting proxy had all given up on that connection minutes earlier, and
the page showed "Load failed" - after the tokens had been spent.

No timeout tuning fixes that. A twelve-minute HTTP response is not a
thing a mobile browser behind a CDN will hold open, so the work has to
stop being an HTTP response: POST starts a JOB and returns its id
immediately, the job runs on its own thread, and the page polls for
progress and collects the result when it is ready. A dropped connection,
a locked phone or a closed tab then costs nothing - the run continues and
the answer is still there to be picked up.

Two further properties matter as much as the plumbing:

- **An identical request joins the running job instead of starting a
  second one.** Double-tapping "Analyse" used to buy the same analysis
  twice. `find_running()` is what stops that, and it is keyed on what the
  run actually is (kind + instrument + timeframe), not on who asked.
- **Progress is recorded as it happens.** "It is working, and here is what
  it just did" is the difference between waiting and not knowing whether
  anything is happening at all.

Jobs live in process memory: this is a single-process dashboard, and a
restart loses them. That is acceptable because it is not where the
*result* lives - a finished run is written to the analysis cache
(ai_advisor/analysis_store.py), which is what survives a restart and what
makes the tokens already spent reusable.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# Keep the most recent jobs only. A dashboard session produces a handful;
# this is a memory bound, not a policy.
MAX_JOBS = 40

# How long a finished job stays collectable. Long enough that a phone that
# slept through the run still finds its answer; short enough that a day of
# analyses is not held in memory forever.
FINISHED_TTL_SECONDS = 3600.0

# Progress lines kept per job. The tail is what tells you where a run is;
# an unbounded list is a slow leak on a run that never converges.
MAX_PROGRESS_LINES = 200


@dataclass
class Job:
    job_id: str
    kind: str
    # What this run IS - "bybit INJUSDT mtf", "bybit INJUSDT 4h". Two
    # requests with the same label are the same work, which is what makes
    # joining a running job safe.
    label: str
    status: str = "running"          # running | done | error
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    progress: List[Dict[str, Any]] = field(default_factory=list)
    result: Optional[Dict[str, Any]] = None
    error: str = ""

    def note(self, text: str) -> None:
        self.progress.append({"at": time.time(), "text": text})
        if len(self.progress) > MAX_PROGRESS_LINES:
            del self.progress[:-MAX_PROGRESS_LINES]
        self.updated_at = time.time()

    def snapshot(self, include_result: bool = True) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "elapsed_seconds": round((self.updated_at if self.status != "running"
                                      else time.time()) - self.created_at, 1),
            "progress": list(self.progress),
            "error": self.error,
            "result": self.result if include_result else None,
        }


_jobs: Dict[str, Job] = {}
_lock = threading.Lock()


def _prune_locked() -> None:
    now = time.time()
    stale = [job_id for job_id, job in _jobs.items()
             if job.status != "running" and now - job.updated_at > FINISHED_TTL_SECONDS]
    for job_id in stale:
        del _jobs[job_id]
    if len(_jobs) > MAX_JOBS:
        finished = sorted((j for j in _jobs.values() if j.status != "running"),
                          key=lambda j: j.updated_at)
        for job in finished[:len(_jobs) - MAX_JOBS]:
            _jobs.pop(job.job_id, None)


def find_running(kind: str, label: str) -> Optional[Job]:
    """The job already doing exactly this work, if there is one.

    This is the token guard: a second "Analyse" tap, a retried request
    after a dropped connection, or two open tabs must attach to the run in
    flight rather than buy the same analysis again."""
    with _lock:
        for job in _jobs.values():
            if job.status == "running" and job.kind == kind and job.label == label:
                return job
    return None


def get(job_id: str) -> Optional[Job]:
    with _lock:
        return _jobs.get(job_id)


def start(kind: str, label: str, work: Callable[[Callable[[str], None]], Dict[str, Any]]) -> Job:
    """Run `work` on its own thread and return the job tracking it.

    `work` is handed a `note(text)` callable to report progress with; what
    it returns becomes the job's result. An exception is caught and kept
    as the job's error - a failed run must be collectable and readable,
    not a silent disappearance."""
    job = Job(job_id=uuid.uuid4().hex[:16], kind=kind, label=label)
    with _lock:
        _prune_locked()
        _jobs[job.job_id] = job

    def run() -> None:
        try:
            job.result = work(job.note)
            job.status = "done"
        except Exception as exc:          # noqa: BLE001 - the job IS the error boundary
            job.error = f"{type(exc).__name__}: {exc}"
            job.status = "error"
            job.note(f"Failed: {job.error}")
        finally:
            job.updated_at = time.time()

    thread = threading.Thread(target=run, name=f"t3-job-{kind}-{job.job_id}", daemon=True)
    thread.start()
    return job


def running_jobs() -> List[Dict[str, Any]]:
    with _lock:
        return [job.snapshot(include_result=False) for job in _jobs.values()
                if job.status == "running"]


def clear_all() -> None:
    """Tests only - jobs are process-global state."""
    with _lock:
        _jobs.clear()
