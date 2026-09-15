"""The Lead Engine's own tables. Nothing it writes touches the analyser's.

Tables, all prefixed so they can never be confused with the older
system's `analysis_cache`, `candles`, `ai_trade_events` and friends:

    lead_engine_features       periodic feature snapshots
    lead_engine_signals        every state transition
    lead_engine_liquidations   forced closes, as they arrive
    lead_engine_sessions       one row per engine start
    lead_engine_backtests      replay results

The ONE thing shared with the rest of the project is the PostgREST
transport in t3_engine/database/supabase_rest.py - the credential, the
base URL and the four verbs. That is shared infrastructure in the same
sense as the web process and the logger, and the specification allows it
explicitly ("Общими могут быть: ... база"). It is also the only import in
this package that crosses the line, which is what keeps the boundary
checkable: one import, named here, and a test asserts there are no others.

Writes are buffered and flushed in batches, and a failure to store is
never allowed to reach the stream thread. Losing a feature row costs a
row of history; losing the tick that was being processed when it failed
would cost the book its sequence.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

# The single permitted shared-infrastructure import. See the docstring.
from t3_engine.database import supabase_rest

logger = logging.getLogger(__name__)

TABLE_FEATURES = "lead_engine_features"
TABLE_SIGNALS = "lead_engine_signals"
TABLE_LIQUIDATIONS = "lead_engine_liquidations"
TABLE_SESSIONS = "lead_engine_sessions"
TABLE_BACKTESTS = "lead_engine_backtests"

ALL_TABLES = (TABLE_FEATURES, TABLE_SIGNALS, TABLE_LIQUIDATIONS,
              TABLE_SESSIONS, TABLE_BACKTESTS)

# Rows are pushed in batches this size. A realtime engine that made one
# HTTP call per feature frame would spend more time in the network stack
# than in its own arithmetic.
BATCH_SIZE = 50

# ...and flushed at least this often even when the batch is not full, so a
# quiet symbol's rows are not held indefinitely.
FLUSH_INTERVAL_SECONDS = 15.0


@dataclass
class Storage:
    """Buffered writer. Degrades to in-memory when Supabase is not
    configured, so nothing in this package needs to know whether storage
    exists - a local run keeps its rows in the buffer and says so."""

    enabled: bool = True
    buffers: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    written: Dict[str, int] = field(default_factory=dict)
    failures: int = 0
    last_error: str = ""
    last_flush: float = field(default_factory=time.time)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def configured(self) -> bool:
        return bool(self.enabled and supabase_rest.configured())

    def _buffer(self, table: str) -> List[Dict[str, Any]]:
        return self.buffers.setdefault(table, [])

    def record(self, table: str, row: Dict[str, Any]) -> None:
        if table not in ALL_TABLES:
            raise ValueError(f"{table!r} is not a Lead Engine table; this module writes "
                             f"only to {', '.join(ALL_TABLES)}")
        with self._lock:
            self._buffer(table).append(row)
            due = (len(self._buffer(table)) >= BATCH_SIZE
                   or time.time() - self.last_flush > FLUSH_INTERVAL_SECONDS)
        if due:
            self.flush(table)

    # ---- the row shapes ----

    def record_features(self, symbol: str, frame: Dict[str, Any]) -> None:
        pressure = frame.get("pressure") or {}
        book = frame.get("orderbook") or {}
        prebreak = frame.get("prebreak") or {}
        self.record(TABLE_FEATURES, {
            "symbol": symbol,
            "at": int(float(frame.get("generated_at") or time.time()) * 1000),
            "price": frame.get("price"),
            "long_pressure": pressure.get("long_pressure"),
            "short_pressure": pressure.get("short_pressure"),
            "obi5": (book.get("obi") or {}).get("obi5"),
            "microprice": book.get("microprice"),
            "cvd": (frame.get("cvd") or {}).get("cvd"),
            "prebreak_long": (prebreak.get("long") or {}).get("break_probability"),
            "prebreak_short": (prebreak.get("short") or {}).get("break_probability"),
            "signal_state": (frame.get("signal") or {}).get("state"),
            "health": (frame.get("health") or {}).get("status"),
        })

    def record_signal(self, symbol: str, snapshot: Dict[str, Any]) -> None:
        self.record(TABLE_SIGNALS, {
            "symbol": symbol,
            "at": int(float(snapshot.get("changed_at") or time.time()) * 1000),
            "state": snapshot.get("state"),
            "previous_state": snapshot.get("previous_state"),
            "direction": snapshot.get("direction"),
            "confidence": snapshot.get("confidence"),
            "break_probability": snapshot.get("break_probability"),
            "level": snapshot.get("level"),
            "reason": snapshot.get("reason"),
        })

    def record_liquidation(self, symbol: str, event: Dict[str, Any]) -> None:
        self.record(TABLE_LIQUIDATIONS, {"symbol": symbol, **event})

    def record_session(self, row: Dict[str, Any]) -> None:
        self.record(TABLE_SESSIONS, row)
        self.flush(TABLE_SESSIONS)

    def record_backtest(self, row: Dict[str, Any]) -> None:
        self.record(TABLE_BACKTESTS, row)
        self.flush(TABLE_BACKTESTS)

    # ---- flushing ----

    def flush(self, table: Optional[str] = None) -> int:
        """Push buffered rows. Returns how many were written.

        Never raises. A storage failure puts the rows BACK in the buffer
        so a transient outage does not lose them, up to a bound - past
        which the oldest are dropped, because an unbounded retry buffer in
        a long-lived process is a memory leak with good intentions."""
        tables = [table] if table else list(self.buffers)
        written = 0
        for name in tables:
            with self._lock:
                rows = self._buffer(name)
                pending, self.buffers[name] = rows[:], []
            if not pending:
                continue
            if not self.configured():
                # Nothing to write to. Keep a bounded tail so a local run
                # can still show what it would have stored.
                with self._lock:
                    self.buffers[name] = (self.buffers[name] + pending)[-500:]
                continue
            try:
                supabase_rest.insert(name, pending)
                written += len(pending)
                with self._lock:
                    self.written[name] = self.written.get(name, 0) + len(pending)
            except Exception as exc:             # noqa: BLE001 - see docstring
                self.failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("lead_engine storage: %s failed: %s", name, self.last_error)
                with self._lock:
                    self.buffers[name] = (pending + self.buffers[name])[-1000:]
        self.last_flush = time.time()
        return written

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "configured": self.configured(),
                "buffered": {name: len(rows) for name, rows in self.buffers.items()},
                "written": dict(self.written),
                "failures": self.failures,
                "last_error": self.last_error,
                "tables": list(ALL_TABLES),
            }


SCHEMA_SQL = """
-- Market Lead Engine storage. Separate tables, separate prefix; nothing
-- here alters the analyser's schema. Run once against the project's
-- Supabase database.

create table if not exists lead_engine_sessions (
    id bigserial primary key,
    started_at bigint not null,
    symbols text,
    config jsonb,
    note text
);

create table if not exists lead_engine_features (
    id bigserial primary key,
    symbol text not null,
    at bigint not null,
    price double precision,
    long_pressure double precision,
    short_pressure double precision,
    obi5 double precision,
    microprice double precision,
    cvd double precision,
    prebreak_long double precision,
    prebreak_short double precision,
    signal_state text,
    health text
);
create index if not exists lead_engine_features_symbol_at
    on lead_engine_features (symbol, at desc);

create table if not exists lead_engine_signals (
    id bigserial primary key,
    symbol text not null,
    at bigint not null,
    state text,
    previous_state text,
    direction text,
    confidence double precision,
    break_probability double precision,
    level double precision,
    reason text
);
create index if not exists lead_engine_signals_symbol_at
    on lead_engine_signals (symbol, at desc);

create table if not exists lead_engine_liquidations (
    id bigserial primary key,
    symbol text not null,
    at bigint not null,
    side text,
    price double precision,
    quantity double precision,
    notional double precision
);
create index if not exists lead_engine_liquidations_symbol_at
    on lead_engine_liquidations (symbol, at desc);

create table if not exists lead_engine_backtests (
    id bigserial primary key,
    created_at bigint not null,
    symbol text,
    events integer,
    precision_pct double precision,
    recall_pct double precision,
    false_positives integer,
    median_lead_seconds double precision,
    mfe double precision,
    mae double precision,
    expected_value double precision,
    detail jsonb
);

-- Row level security on, matching the rest of this project: the service
-- key bypasses it, the publishable key reaches nothing.
alter table lead_engine_sessions     enable row level security;
alter table lead_engine_features     enable row level security;
alter table lead_engine_signals      enable row level security;
alter table lead_engine_liquidations enable row level security;
alter table lead_engine_backtests    enable row level security;
"""


class Recorder:
    """Files what the engine sees, whether or not anyone is looking.

    Without this the engine is only persisted when a browser asks for a
    frame: `api.state()` files a feature row, and nothing else writes at
    all. That is fine for a dashboard and wrong for a monitor. A signal
    that fired at 03:00 with the tab closed left no trace, the
    liquidation tables stayed permanently empty, and a replay could only
    ever be built from a capture somebody remembered to take.

    So a thread walks the symbols on its own clock and writes three
    things: a feature row per symbol per interval, every signal
    TRANSITION as it happens (not the state each tick - that would be the
    same row several times a second), and liquidation events once each.

    It never touches the stream thread and never raises into it: the
    engine's snapshot is read through the same facade any other caller
    uses, and every failure is swallowed into the storage buffer's own
    accounting.
    """

    # How often a feature row is written per symbol. Fifteen seconds is
    # about the granularity a pre-break setup develops at; a row per
    # frame would be four a second per symbol and would tell no one
    # anything more.
    DEFAULT_INTERVAL_SECONDS = 15.0

    def __init__(self, engine, storage: "Storage",
                 interval_seconds: float = DEFAULT_INTERVAL_SECONDS) -> None:
        self.engine = engine
        self.storage = storage
        self.interval_seconds = max(2.0, float(interval_seconds))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_signal: Dict[str, float] = {}
        self._last_liquidation: Dict[str, int] = {}
        self.sweeps = 0
        self.rows = 0
        self._last_heartbeat: Optional[Tuple[float, int]] = None
        # Rolling book-age samples, for the percentiles BUILD-CHECK-044
        # asks for. Thirty minutes at one sweep per symbol per fifteen
        # seconds is a few hundred numbers - small enough to keep, large
        # enough to mean something.
        self._book_ages: Deque[float] = deque(maxlen=2_000)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="lead-engine-recorder",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.storage.flush()

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def sweep_once(self) -> int:
        """One pass over every symbol. Exposed so a test can drive it
        without a thread or a clock. Returns rows written to the buffer."""
        written = 0
        self.sweeps += 1
        for symbol in list(self.engine.states):
            state = self.engine.states.get(symbol)
            if state is None:
                continue
            try:
                frame = state.snapshot()
            except Exception:                    # noqa: BLE001 - a symbol
                continue                         # that cannot be read is skipped
            try:
                self._note_freshness(symbol, frame)
                self.storage.record_features(symbol, frame)
                written += 1
                written += self._record_transition(symbol, state)
                written += self._record_liquidations(symbol, state)
            except Exception:                    # noqa: BLE001 - storage
                logger.debug("lead_engine recorder: %s not filed", symbol, exc_info=True)
        self.rows += written
        self._heartbeat()
        return written

    @staticmethod
    def _percentile(values: List[float], fraction: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
        return ordered[index]

    def _note_freshness(self, symbol: str, frame: Dict[str, Any]) -> None:
        health = frame.get("health") or {}
        age = health.get("book_age_ms")
        if isinstance(age, (int, float)):
            self._book_ages.append(float(age))

    def _heartbeat(self) -> None:
        """One line a sweep with the socket's own counters.

        The ingest rate is otherwise only visible to whoever can reach
        `/api/lead-engine/status` in a browser, which is no help when the
        question is "what was it doing an hour ago". Rate is computed
        between sweeps rather than since start, so a quiet patch shows as
        a quiet patch instead of being averaged away."""
        stream = getattr(self.engine, "stream", None)
        if stream is None:
            return
        stats = stream.stats
        now = time.time()
        messages = int(getattr(stats, "messages", 0) or 0)
        rate = None
        if self._last_heartbeat is not None:
            previous_time, previous_messages = self._last_heartbeat
            elapsed = now - previous_time
            if elapsed > 0:
                rate = (messages - previous_messages) / elapsed
        self._last_heartbeat = (now, messages)
        snapshot = stats.as_dict()
        ages = list(self._book_ages)
        gaps = resyncs = 0
        # Not a count. A bare `data_failure=1` across seven symbols says
        # something is wrong and nothing about what, which is how one
        # flickering symbol looked identical to a feed-wide problem for
        # three heartbeats. The symbol and its reason go in the line.
        failing: List[str] = []
        for state in list(self.engine.states.values()):
            sequence = getattr(state.book, "sequence_stats", None)
            if sequence is not None:
                counters = sequence()
                gaps += int(counters.get("sequence_gaps") or 0)
                resyncs += int(counters.get("resync_count") or 0)
            current = getattr(getattr(state, "signals", None), "current", None)
            if current is not None and getattr(current, "state", "") == "DATA_FAILURE":
                reason = str(getattr(current, "reason", "") or "").strip()
                failing.append(f"{state.symbol}({reason})" if reason else state.symbol)
        failures = len(failing)
        logger.info(
            "lead_engine feed: connected=%s messages=%d rate=%s/s "
            "net_latency=%.1fms queue_wait=%.1fms queue=%d/%d dropped=%d "
            "book_age_median=%.0fms book_age_p95=%.0fms samples=%d "
            "gaps=%d resyncs=%d data_failure=%d%s connects=%d reconnects=%d "
            "topics=%d symbols=%d",
            bool(snapshot.get("connected")), messages,
            "?" if rate is None else f"{rate:.1f}",
            float(snapshot.get("network_latency_ms") or 0.0),
            float(snapshot.get("queue_latency_ms") or 0.0),
            int(snapshot.get("queue_depth") or 0),
            int(snapshot.get("queue_limit") or 0),
            int(snapshot.get("dropped") or 0),
            self._percentile(ages, 0.5), self._percentile(ages, 0.95), len(ages),
            gaps, resyncs, failures,
            (" [" + "; ".join(failing) + "]") if failing else "",
            int(snapshot.get("connects") or 0),
            int(snapshot.get("reconnects") or 0),
            int(snapshot.get("subscribed_topics") or 0),
            len(self.engine.states))

    def _record_transition(self, symbol: str, state) -> int:
        """Only when the state actually CHANGED.

        `SignalMachine.current.changed_at` does not move while a state
        persists (deliberately - see signal_machine), which is exactly the
        marker needed here: one row per transition, not one per tick."""
        current = state.signals.current
        stamp = float(current.changed_at or 0.0)
        if not stamp or self._last_signal.get(symbol) == stamp:
            return 0
        self._last_signal[symbol] = stamp
        self.storage.record_signal(symbol, current.as_dict())
        return 1

    def _record_liquidations(self, symbol: str, state) -> int:
        """Events newer than the last one filed, so a restart does not
        re-file the window the engine is still holding in memory."""
        since = self._last_liquidation.get(symbol, 0)
        newest = since
        written = 0
        for item in state.liquidations.events.all():
            stamp = int(getattr(item, "timestamp_ms", 0))
            if stamp <= since:
                continue
            self.storage.record_liquidation(symbol, {
                "at": stamp,
                "side": "long" if getattr(item, "is_long", False) else "short",
                "price": float(getattr(item, "price", 0.0)),
                "quantity": float(getattr(item, "quantity", 0.0)),
                "notional": float(getattr(item, "notional", 0.0)),
            })
            written += 1
            newest = max(newest, stamp)
        self._last_liquidation[symbol] = newest
        return written

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sweep_once()
                self.storage.flush()
            except Exception:                    # noqa: BLE001 - a recorder
                logger.exception("lead_engine recorder sweep failed")   # that dies
            self._stop.wait(self.interval_seconds)                      # is worse

    def stats(self) -> Dict[str, Any]:
        return {"running": self.running(), "sweeps": self.sweeps,
                "rows_buffered": self.rows,
                "interval_seconds": self.interval_seconds}
