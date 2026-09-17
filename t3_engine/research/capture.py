"""Record the real Bybit microstructure stream, within a free-tier budget.

WHY THIS EXISTS. The only bytes of market data this project had were
`tests/fixtures/bybit_capture_injusdt.jsonl.gz`, and that file is not a
capture: every timestamp sits on an exact 500ms grid starting at
1700000000000, and every BTCUSDT level holds exactly size 8. It is a
hand-built scenario - excellent as a deterministic pipeline regression,
worthless as evidence about how a market behaves or what an order would
have paid. Nothing about execution can be learned from it.

`lead_engine_features` is not a substitute either. It is ONE ROW EVERY
FIFTEEN SECONDS of already-computed scores. You cannot reconstruct a
spread, a queue, or the path a price took between two rows from that, so
you cannot price a fill from it. Anything claiming to be a backtest of an
execution strategy on that table is claiming more than the data holds.

So this records the stream itself: order book snapshots and the deltas
that follow them, public trades with the aggressor side, and the two
clocks (exchange and local receive) that make causality checkable.

WHAT IT DELIBERATELY DOES NOT GIVE YOU. Bybit's public book is
AGGREGATED per price level. It tells you 124.64 contracts rest at 5.759;
it does not tell you how many orders that is, whose they are, or where in
that queue ours would sit. Every maker fill estimated from this data is
an ESTIMATE, and the simulator says so by carrying a conservative and an
optimistic variant rather than one number pretending to be the truth.

THE BUDGET IS A HARD STOP, NOT A GOAL. Supabase's free tier is 500MB of
database. A recorder that discovers this by filling it has destroyed the
thing it was feeding. So bytes are counted as they are written, the
budget is checked BEFORE each flush, and the recorder parks itself in
EXHAUSTED rather than write one row past it.

DROPS ARE COUNTED, NEVER SILENT. The buffer is bounded. When it
overflows, the oldest frames go - the same rule the ingest queue uses -
and the count travels with the segment that follows, so a gap in the data
is visible in the data rather than inferred from its absence.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

TABLE_CAPTURES = "research_captures"

# Environment switches. Off by default: a recorder that starts itself on
# every deploy is a recorder that fills a quota nobody asked it to spend.
ENABLED_ENV = "RESEARCH_CAPTURE_ENABLED"
ENABLED_ENV_PREFIXED = "T3_RESEARCH_CAPTURE_ENABLED"
SYMBOLS_ENV = "RESEARCH_CAPTURE_SYMBOLS"
SYMBOLS_ENV_PREFIXED = "T3_RESEARCH_CAPTURE_SYMBOLS"
BUDGET_ENV = "RESEARCH_CAPTURE_BUDGET_MB"
BUDGET_ENV_PREFIXED = "T3_RESEARCH_CAPTURE_BUDGET_MB"

_TRUE = {"1", "true", "yes", "on"}

# How much stored (compressed, base64) data this recorder may ever write.
# Supabase free is 500MB for the WHOLE database, and the engine's own
# tables live there too, so this claims a minority share by default.
DEFAULT_BUDGET_MB = 120.0

# A segment is one row. Big enough that the per-row overhead is noise,
# small enough that losing the tail of one costs seconds rather than
# minutes, and well inside any statement size limit after compression.
FRAMES_PER_SEGMENT = 1500
SECONDS_PER_SEGMENT = 60.0

# The buffer between the ingest thread and the flush thread. At the
# observed ~10 frames/s/symbol this is minutes of slack.
BUFFER_LIMIT = 20_000

# Topics worth the bytes. The book and the tape are the two things an
# execution model actually reads; tickers and liquidations are cheap and
# carry the funding rate and the flush events hypothesis C needs. Klines
# are NOT recorded - they are derivable from the tape and would double
# the bill.
RECORDED_PREFIXES = ("orderbook", "publicTrade", "tickers", "allLiquidation")

# COALESCING, AND WHY IT IS NOT A LOSS OF INFORMATION.
#
# Measured on the first two minutes of real recording: tickers were 31%
# of frames and, being fat objects, over half the bytes - for a 1Hz
# republication of the mark price and a funding rate that changes every
# eight hours. Order book deltas were another 37%.
#
# Book deltas are MERGED rather than sampled. Merging is exact: a delta
# says "this price now holds this size", so the last word within a window
# is the whole truth about that window's end, and replaying the merged
# stream reconstructs the book EXACTLY as it stood at each boundary. What
# is lost is time resolution between boundaries, not accuracy at them.
#
# 100ms is chosen against what this deployment can act on. A decision
# here reaches the exchange in something like 150-250ms; book detail
# finer than that cannot inform a tradeable decision, so recording it
# would spend the quota on resolution no strategy could use. Anything
# claiming an edge inside 100ms is out of scope for this hosting and the
# data says so rather than implying otherwise.
#
# Trades and liquidations are NEVER coalesced. They are 3% of the frames
# and they are the information-bearing events - the tape is what fills a
# maker and what shows aggression - so they are kept one for one.
COALESCE_BOOK_MS = 100
COALESCE_TICKER_MS = 5_000
NEVER_COALESCED = ("publicTrade", "allLiquidation")


def _env(name: str, prefixed: str, default: str = "") -> str:
    for candidate in (name, prefixed):
        value = os.getenv(candidate, "").strip()
        if value:
            return value
    return default


def enabled() -> bool:
    return _env(ENABLED_ENV, ENABLED_ENV_PREFIXED).lower() in _TRUE


def configured_symbols() -> List[str]:
    raw = _env(SYMBOLS_ENV, SYMBOLS_ENV_PREFIXED)
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def configured_budget_mb() -> float:
    raw = _env(BUDGET_ENV, BUDGET_ENV_PREFIXED)
    try:
        return max(0.0, float(raw)) if raw else DEFAULT_BUDGET_MB
    except ValueError:
        return DEFAULT_BUDGET_MB


@dataclass
class Segment:
    """One flushed row, and everything needed to trust it later."""
    segment_id: str
    session_id: str
    symbol: str
    seq: int
    frames: int
    dropped_before: int
    first_exchange_ms: int
    last_exchange_ms: int
    first_recv_ms: int
    last_recv_ms: int
    topics: Dict[str, int]
    raw_bytes: int
    stored_bytes: int
    sha256: str
    payload: str

    def to_row(self) -> Dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "session_id": self.session_id,
            "symbol": self.symbol,
            "seq": self.seq,
            "frames": self.frames,
            "dropped_before": self.dropped_before,
            "first_exchange_ms": self.first_exchange_ms,
            "last_exchange_ms": self.last_exchange_ms,
            "first_recv_ms": self.first_recv_ms,
            "last_recv_ms": self.last_recv_ms,
            "topics": self.topics,
            "raw_bytes": self.raw_bytes,
            "stored_bytes": self.stored_bytes,
            "sha256": self.sha256,
            "payload": self.payload,
        }


@dataclass
class CaptureStats:
    state: str = "IDLE"
    frames_seen: int = 0
    frames_buffered: int = 0
    frames_written: int = 0
    frames_dropped: int = 0
    segments_written: int = 0
    frames_coalesced: int = 0
    write_failures: int = 0
    raw_bytes: int = 0
    stored_bytes: int = 0
    budget_bytes: int = 0
    started_at: float = 0.0
    last_write_at: float = 0.0
    last_error: str = ""

    def as_dict(self) -> Dict[str, Any]:
        used = self.stored_bytes
        budget = self.budget_bytes or 1
        return {
            "state": self.state,
            "frames_seen": self.frames_seen,
            "frames_buffered": self.frames_buffered,
            "frames_written": self.frames_written,
            "frames_dropped": self.frames_dropped,
            "segments_written": self.segments_written,
            "frames_coalesced": self.frames_coalesced,
            "write_failures": self.write_failures,
            "raw_mb": round(self.raw_bytes / 1e6, 3),
            "stored_mb": round(self.stored_bytes / 1e6, 3),
            "budget_mb": round(self.budget_bytes / 1e6, 1),
            "budget_used_pct": round(100.0 * used / budget, 2),
            "compression": round(self.raw_bytes / max(1, self.stored_bytes), 2),
            "uptime_seconds": round(time.time() - self.started_at, 1) if self.started_at else 0.0,
            "last_write_at": self.last_write_at,
            "last_error": self.last_error,
        }


class CaptureRecorder:
    """Tap the engine's frame stream and persist it in bounded segments.

    The tap is `observe()`. It runs on the ingest thread, so it does the
    least possible work: a prefix test, a dict append, a counter. Every
    byte of compression and every network call happens on this object's
    own thread.
    """

    def __init__(self, symbols: Sequence[str], *,
                 session_id: Optional[str] = None,
                 budget_mb: Optional[float] = None,
                 frames_per_segment: int = FRAMES_PER_SEGMENT,
                 seconds_per_segment: float = SECONDS_PER_SEGMENT,
                 buffer_limit: int = BUFFER_LIMIT,
                 writer: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.symbols = {s.upper() for s in symbols}
        self.session_id = session_id or f"cap-{int(clock())}"
        self.frames_per_segment = frames_per_segment
        self.seconds_per_segment = seconds_per_segment
        self.buffer_limit = buffer_limit
        self._clock = clock
        self._writer = writer or _supabase_writer
        self._lock = threading.Lock()
        self._buffer: Deque[Dict[str, Any]] = deque()
        self._dropped_since_flush = 0
        self._seq = 0
        self._segment_opened_at = clock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.coalesce_book_ms = COALESCE_BOOK_MS
        self.coalesce_ticker_ms = COALESCE_TICKER_MS
        # symbol -> the delta being merged for the current window
        self._pending_delta: Dict[str, Dict[str, Any]] = {}
        self._last_ticker_ms: Dict[str, int] = {}
        self._last_frames: List[Dict[str, Any]] = []
        budget = DEFAULT_BUDGET_MB if budget_mb is None else budget_mb
        self.stats = CaptureStats(budget_bytes=int(budget * 1e6))

    # ---- the tap ------------------------------------------------------

    def observe(self, topic: str, message: Dict[str, Any],
                received_at_ms: int) -> None:
        """Called from the ingest thread for every routed frame.

        Cheap by contract. If this ever grows a JSON dump or a hash it
        stops being a tap and becomes a second ingest pipeline competing
        with the first one for the GIL."""
        if self.stats.state not in ("RECORDING",):
            return
        head = topic.split(".", 1)[0]
        if head not in RECORDED_PREFIXES:
            return
        symbol = topic.rsplit(".", 1)[-1].upper()
        if symbol not in self.symbols:
            return
        with self._lock:
            self.stats.frames_seen += 1
            frame = {
                "topic": topic,
                "ts": int(message.get("ts") or 0),
                "r": received_at_ms,
                "type": message.get("type") or "",
                "data": message.get("data"),
            }
            for ready in self._coalesce(head, symbol, frame):
                self._append(ready)
            self.stats.frames_buffered = len(self._buffer)

    def _append(self, frame: Dict[str, Any]) -> None:
        """Buffer one frame, dropping the oldest if the buffer is full.

        Same rule as the ingest queue: a recorder that blocks the ingest
        thread to stay complete has broken the thing it was recording."""
        if len(self._buffer) >= self.buffer_limit:
            self._buffer.popleft()
            self._dropped_since_flush += 1
            self.stats.frames_dropped += 1
        self._buffer.append(frame)

    def _coalesce(self, head: str, symbol: str,
                  frame: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Return the frames that are now ready to buffer.

        Called with the lock held. Trades and liquidations pass straight
        through; a book delta is MERGED into the window it belongs to and
        emitted when the window rolls; a snapshot flushes whatever was
        pending and goes out on its own, because a snapshot is a new
        starting point and merging it into a delta would lose that."""
        if head in NEVER_COALESCED:
            return [frame]

        if head == "tickers":
            if not self.coalesce_ticker_ms:
                return [frame]
            last = self._last_ticker_ms.get(symbol, 0)
            if frame["r"] - last < self.coalesce_ticker_ms:
                self.stats.frames_coalesced += 1
                return []
            self._last_ticker_ms[symbol] = frame["r"]
            return [frame]

        if head != "orderbook" or not self.coalesce_book_ms:
            return [frame]

        pending = self._pending_delta.get(symbol)
        if (frame["type"] or "").lower() == "snapshot":
            out = []
            if pending is not None:
                out.append(_pending_to_frame(pending))
                self._pending_delta.pop(symbol, None)
            out.append(frame)
            return out

        bucket = frame["r"] // self.coalesce_book_ms
        out = []
        if pending is not None and pending["bucket"] != bucket:
            out.append(_pending_to_frame(pending))
            pending = None
        if pending is None:
            pending = {"bucket": bucket, "topic": frame["topic"], "b": {},
                       "a": {}, "ts": frame["ts"], "r": frame["r"], "u": 0,
                       "merged": 0}
            self._pending_delta[symbol] = pending
        else:
            self.stats.frames_coalesced += 1

        data = frame.get("data") or {}
        # A delta states the size a price now holds, so the LAST word in
        # the window is the whole truth about the window's end.
        for price, size in (data.get("b") or []):
            pending["b"][price] = size
        for price, size in (data.get("a") or []):
            pending["a"][price] = size
        pending["ts"] = frame["ts"] or pending["ts"]
        pending["r"] = frame["r"]
        pending["u"] = int(data.get("u") or pending["u"])
        pending["merged"] += 1
        return out

    def _drain_pending(self) -> None:
        """Called with the lock held, before a flush, so a segment never
        ends in the middle of a merge window."""
        for symbol in list(self._pending_delta):
            self._append(_pending_to_frame(self._pending_delta.pop(symbol)))

    # ---- lifecycle ----------------------------------------------------

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        if not self.symbols:
            self.stats.state = "IDLE"
            self.stats.last_error = "no symbols configured"
            return False
        if self.stats.stored_bytes >= self.stats.budget_bytes:
            self.stats.state = "EXHAUSTED"
            return False
        self._stop.clear()
        self.stats.state = "RECORDING"
        self.stats.started_at = self._clock()
        self._segment_opened_at = self._clock()
        self._thread = threading.Thread(target=self._run, name="research-capture",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self, flush: bool = True) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=10.0)
        if flush:
            self.flush(force=True)
        if self.stats.state == "RECORDING":
            self.stats.state = "STOPPED"

    def _run(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(1.0)
            if self._stop.is_set():
                break
            try:
                self.flush()
            except Exception as exc:                      # never kill the thread
                self.stats.write_failures += 1
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("research capture flush failed: %s", exc)

    # ---- flushing -----------------------------------------------------

    def _should_flush(self, now: float) -> bool:
        if not self._buffer:
            return False
        if len(self._buffer) >= self.frames_per_segment:
            return True
        return (now - self._segment_opened_at) >= self.seconds_per_segment

    def flush(self, force: bool = False) -> Optional[Segment]:
        now = self._clock()
        with self._lock:
            self._drain_pending()
            if not force and not self._should_flush(now):
                return None
            if not self._buffer:
                return None
            frames = list(self._buffer)
            dropped = self._dropped_since_flush
            self._buffer.clear()
            self._dropped_since_flush = 0
            self.stats.frames_buffered = 0
            self._segment_opened_at = now
            self._seq += 1
            seq = self._seq

        self._last_frames = frames          # for tests and for a post-mortem
        segment = self._build(frames, seq, dropped)

        # The budget is checked against what this row WOULD cost, before
        # it is written. Checking afterwards is how a quota gets passed.
        if self.stats.stored_bytes + segment.stored_bytes > self.stats.budget_bytes:
            self.stats.state = "EXHAUSTED"
            logger.warning("research capture budget exhausted at %.1fMB; not writing "
                           "segment %s", self.stats.stored_bytes / 1e6, segment.segment_id)
            return None

        try:
            self._writer([segment.to_row()])
        except Exception as exc:
            self.stats.write_failures += 1
            self.stats.last_error = f"{type(exc).__name__}: {exc}"
            raise

        self.stats.segments_written += 1
        self.stats.frames_written += segment.frames
        self.stats.raw_bytes += segment.raw_bytes
        self.stats.stored_bytes += segment.stored_bytes
        self.stats.last_write_at = now
        return segment

    def _build(self, frames: List[Dict[str, Any]], seq: int,
               dropped: int) -> Segment:
        symbol = sorted({f["topic"].rsplit(".", 1)[-1].upper() for f in frames})
        topics: Dict[str, int] = {}
        for frame in frames:
            head = frame["topic"].split(".", 1)[0]
            topics[head] = topics.get(head, 0) + 1
        body = "\n".join(json.dumps(f, separators=(",", ":")) for f in frames)
        raw = body.encode("utf-8")
        packed = gzip.compress(raw, compresslevel=9)
        payload = base64.b64encode(packed).decode("ascii")
        exchange = [f["ts"] for f in frames if f["ts"]]
        recv = [f["r"] for f in frames if f["r"]]
        return Segment(
            segment_id=f"{self.session_id}:{seq:06d}",
            session_id=self.session_id,
            symbol=",".join(symbol),
            seq=seq,
            frames=len(frames),
            dropped_before=dropped,
            first_exchange_ms=min(exchange) if exchange else 0,
            last_exchange_ms=max(exchange) if exchange else 0,
            first_recv_ms=min(recv) if recv else 0,
            last_recv_ms=max(recv) if recv else 0,
            topics=topics,
            raw_bytes=len(raw),
            stored_bytes=len(payload),
            # The hash covers the RAW frames, not the base64, so it stays
            # meaningful after a decode and can be checked by anyone
            # holding the decompressed file.
            sha256=hashlib.sha256(raw).hexdigest(),
            payload=payload,
        )


def _pending_to_frame(pending: Dict[str, Any]) -> Dict[str, Any]:
    """The merged window, back in Bybit's own delta shape so a reader
    needs no special case for it. `m` records how many frames went in, so
    the manifest can report the real update rate rather than the
    recorded one."""
    return {
        "topic": pending["topic"], "ts": pending["ts"], "r": pending["r"],
        "type": "delta",
        "data": {"u": pending["u"],
                 "b": [[p, q] for p, q in pending["b"].items()],
                 "a": [[p, q] for p, q in pending["a"].items()]},
        "m": pending["merged"],
    }


def decode_segment(payload: str) -> List[Dict[str, Any]]:
    """The inverse of `_build`, for anything reading a segment back."""
    raw = gzip.decompress(base64.b64decode(payload))
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line]


def verify_segment(row: Dict[str, Any]) -> Tuple[bool, str]:
    """Does the stored payload still hash to what the row claims?"""
    try:
        raw = gzip.decompress(base64.b64decode(row["payload"]))
    except Exception as exc:
        return False, f"undecodable: {type(exc).__name__}: {exc}"
    digest = hashlib.sha256(raw).hexdigest()
    if digest != row.get("sha256"):
        return False, f"sha256 mismatch: stored {row.get('sha256')}, actual {digest}"
    frames = len([line for line in raw.decode("utf-8").splitlines() if line])
    if frames != row.get("frames"):
        return False, f"frame count mismatch: stored {row.get('frames')}, actual {frames}"
    return True, ""


def _supabase_writer(rows: List[Dict[str, Any]]) -> None:
    """Upsert on segment_id: a retry after a timeout must not double-bill
    the budget or duplicate the frames."""
    from t3_engine.database import supabase_rest

    if not supabase_rest.configured():
        raise RuntimeError("Supabase is not configured; refusing to discard frames")
    supabase_rest.insert(TABLE_CAPTURES, rows, on_conflict="segment_id")


_recorder: Optional[CaptureRecorder] = None
_recorder_lock = threading.Lock()


def get_recorder() -> Optional[CaptureRecorder]:
    return _recorder


def start_recorder(symbols: Sequence[str]) -> Optional[CaptureRecorder]:
    """Start the process-wide recorder if the environment asked for one."""
    global _recorder
    with _recorder_lock:
        if _recorder is not None and _recorder.stats.state == "RECORDING":
            return _recorder
        if not enabled():
            return None
        wanted = configured_symbols() or list(symbols)
        chosen = [s for s in wanted if s.upper() in {x.upper() for x in symbols}]
        if not chosen:
            logger.warning("research capture enabled but no configured symbol is "
                           "subscribed; not starting")
            return None
        _recorder = CaptureRecorder(chosen, budget_mb=configured_budget_mb())
        if _recorder.start():
            logger.info("research capture recording %s, budget %.0fMB",
                        ",".join(chosen), configured_budget_mb())
        return _recorder


def reset_recorder() -> None:
    global _recorder
    with _recorder_lock:
        if _recorder is not None:
            _recorder.stop(flush=False)
        _recorder = None
