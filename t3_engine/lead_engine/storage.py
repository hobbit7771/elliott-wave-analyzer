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
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

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
