"""Run experiments where the data is.

This project is driven from a sandbox whose egress reaches neither Bybit
nor Supabase. The Render service reaches both. So experiments are QUEUED
as rows and executed there, and the job row is the entire interface: a
spec goes in, a result comes back, and the experiment log is the same
table either way.

It is also the honest way to run this on a free box. The worker takes ONE
job at a time, yields between windows, and refuses a job whose frame
budget is larger than the box can chew, because a backtest that starves
the ingest thread corrupts the live data it is supposed to be studying.

WHAT A WORKER MAY DO: read captures, run strategies through the
simulator, write experiment rows. It cannot place an order, cannot touch
an exchange credential, and cannot reach the live engine's state - the
same isolation the external API has, for the same reason.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

TABLE_JOBS = "research_jobs"
TABLE_EXPERIMENTS = "research_experiments"

PENDING, RUNNING, DONE, FAILED = "pending", "running", "done", "failed"

# A free box has a tenth of a CPU and a live ingest thread that must keep
# up. These are the limits that stop a backtest from being the reason the
# data it studies has gaps.
MAX_FRAMES_PER_JOB = 400_000
YIELD_EVERY_FRAMES = 5_000
YIELD_SECONDS = 0.02
POLL_SECONDS = 20.0
MAX_ATTEMPTS = 3


def _rest():
    from t3_engine.database import supabase_rest
    return supabase_rest


def enqueue(kind: str, spec: Dict[str, Any], job_id: Optional[str] = None) -> str:
    job_id = job_id or f"{kind}-{uuid.uuid4().hex[:10]}"
    _rest().insert(TABLE_JOBS, [{"job_id": job_id, "kind": kind, "spec": spec,
                                 "status": PENDING}], on_conflict="job_id")
    return job_id


def claim_next() -> Optional[Dict[str, Any]]:
    """Take the oldest pending job.

    Single worker per process and one service, so a compare-and-set is
    not needed; the attempt counter is what stops a job that kills the
    process from being retried forever."""
    rows = _rest().select(TABLE_JOBS, filters={"status": PENDING},
                          order="created_at.asc", limit=1)
    if not rows:
        return None
    job = rows[0]
    if int(job.get("attempts") or 0) >= MAX_ATTEMPTS:
        _rest().insert(TABLE_JOBS, [{**job, "status": FAILED,
                                     "error": "too many attempts"}],
                       on_conflict="job_id")
        return None
    _rest().insert(TABLE_JOBS, [{**job, "status": RUNNING,
                                 "attempts": int(job.get("attempts") or 0) + 1,
                                 "claimed_at": _now()}], on_conflict="job_id")
    return job


def finish(job: Dict[str, Any], result: Dict[str, Any]) -> None:
    _rest().insert(TABLE_JOBS, [{**job, "status": DONE, "result": result,
                                 "error": "", "finished_at": _now()}],
                   on_conflict="job_id")


def fail(job: Dict[str, Any], error: str) -> None:
    _rest().insert(TABLE_JOBS, [{**job, "status": FAILED, "error": error[:4000],
                                 "finished_at": _now()}], on_conflict="job_id")


def record_experiment(record) -> None:
    _rest().insert(TABLE_EXPERIMENTS, [record.as_row()],
                   on_conflict="experiment_id")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ResearchWorker:
    """One thread, one job at a time, yielding to the ingest thread."""

    def __init__(self, poll_seconds: float = POLL_SECONDS,
                 handlers: Optional[Dict[str, Callable[[Dict[str, Any]],
                                                       Dict[str, Any]]]] = None
                 ) -> None:
        self.poll_seconds = poll_seconds
        self.handlers = handlers or {}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.jobs_done = 0
        self.jobs_failed = 0
        self.last_error = ""
        self.running_job = ""

    def register(self, kind: str,
                 handler: Callable[[Dict[str, Any]], Dict[str, Any]]) -> None:
        self.handlers[kind] = handler

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="research-worker",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = claim_next()
            except Exception as exc:            # storage down: try again later
                self.last_error = f"claim: {type(exc).__name__}: {exc}"
                job = None
            if job is None:
                self._stop.wait(self.poll_seconds)
                continue
            self.running_job = job.get("job_id", "")
            try:
                handler = self.handlers.get(job.get("kind", ""))
                if handler is None:
                    raise ValueError(f"no handler for kind {job.get('kind')!r}")
                result = handler(job.get("spec") or {})
                finish(job, result)
                self.jobs_done += 1
            except Exception as exc:
                self.jobs_failed += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("research job %s failed", self.running_job)
                try:
                    fail(job, traceback.format_exc())
                except Exception:               # nothing more we can do
                    pass
            finally:
                self.running_job = ""

    def stats(self) -> Dict[str, Any]:
        return {"alive": bool(self._thread and self._thread.is_alive()),
                "jobs_done": self.jobs_done, "jobs_failed": self.jobs_failed,
                "running_job": self.running_job, "last_error": self.last_error,
                "handlers": sorted(self.handlers)}


_worker: Optional[ResearchWorker] = None


def get_worker() -> Optional[ResearchWorker]:
    return _worker


def start_worker() -> Optional[ResearchWorker]:
    global _worker
    if _worker is not None and _worker._thread and _worker._thread.is_alive():
        return _worker
    from t3_engine.research import handlers as research_handlers

    _worker = ResearchWorker(handlers=research_handlers.build())
    _worker.start()
    return _worker


def reset_worker() -> None:
    global _worker
    if _worker is not None:
        _worker.stop()
    _worker = None
