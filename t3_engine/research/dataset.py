"""Load a capture, and say honestly what is in it.

A manifest is not paperwork. The previous round of this project reported
a "replay of 10,288 recorded Bybit frames" as evidence, and the file in
question turned out to be a hand-built scenario: every timestamp on an
exact 500ms grid from 1700000000000, every BTCUSDT level holding exactly
size 8. Nothing about a market could be learned from it and nothing about
execution could be priced against it. The difference between a recording,
a fixture and a repeated recording is the difference between a result and
a story, so `describe()` measures the things that tell them apart -
timestamp granularity, size entropy, the real gaps - rather than
repeating what a filename claims.

`classify()` names what it found. It can and does return SYNTHETIC.
"""

from __future__ import annotations

import collections
import hashlib
import json
import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from t3_engine.research.book import BUY, SELL, BookReconstructor, BookState, Trade
from t3_engine.research.capture import TABLE_CAPTURES, decode_segment, verify_segment

REAL_CAPTURE = "REAL_CAPTURE"
SYNTHETIC = "SYNTHETIC"
SUSPECT = "SUSPECT"


@dataclass
class Event:
    """One thing that happened, in the order this process learned it."""
    recv_ms: int
    exchange_ms: int
    kind: str                     # book | trade | liquidation | ticker
    payload: Any


@dataclass
class DatasetManifest:
    name: str
    source: str
    provenance: str
    classification: str
    symbols: List[str]
    market: str
    segments: int
    frames: int
    dropped: int
    from_ms: int
    to_ms: int
    sha256: str
    topics: Dict[str, int] = field(default_factory=dict)
    quality: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    @property
    def span_seconds(self) -> float:
        return max(0.0, (self.to_ms - self.from_ms) / 1000.0)

    def as_dict(self) -> Dict[str, Any]:
        out = {k: v for k, v in self.__dict__.items()}
        out["span_seconds"] = round(self.span_seconds, 1)
        out["span_hours"] = round(self.span_seconds / 3600.0, 3)
        return out

    def render(self) -> str:
        lines = [
            f"# DATASET_MANIFEST: {self.name}",
            "",
            f"* classification : **{self.classification}**",
            f"* source         : {self.source}",
            f"* provenance     : {self.provenance}",
            f"* market         : {self.market}",
            f"* symbols        : {', '.join(self.symbols) or '(none)'}",
            f"* period (UTC ms): {self.from_ms} -> {self.to_ms} "
            f"({self.span_seconds / 3600.0:.3f} h)",
            f"* segments       : {self.segments}",
            f"* frames         : {self.frames} ({self.dropped} dropped by the recorder)",
            f"* sha256         : {self.sha256}",
            "",
            "## Topic mix",
        ]
        for topic, count in sorted(self.topics.items(), key=lambda kv: -kv[1]):
            lines.append(f"* {topic}: {count}")
        lines += ["", "## Quality"]
        for key, value in sorted(self.quality.items()):
            lines.append(f"* {key}: {value}")
        if self.warnings:
            lines += ["", "## Warnings"]
            lines += [f"* {w}" for w in self.warnings]
        return "\n".join(lines)


def load_segments_from_supabase(session_id: Optional[str] = None,
                                symbol: Optional[str] = None,
                                limit: int = 10_000) -> List[Dict[str, Any]]:
    from t3_engine.database import supabase_rest

    filters: Dict[str, Any] = {}
    if session_id:
        filters["session_id"] = session_id
    if symbol:
        filters["symbol"] = symbol
    return supabase_rest.select(TABLE_CAPTURES, filters=filters or None,
                                order="seq.asc", limit=limit)


def frames_from_segments(rows: Sequence[Dict[str, Any]], *,
                         verify: bool = True) -> Iterator[Dict[str, Any]]:
    """Decode segments in order, checking each one's hash.

    A corrupt segment is SKIPPED LOUDLY rather than silently repaired:
    quietly dropping the frames it held would leave a hole nobody could
    see, and quietly keeping them would trust bytes that failed their own
    checksum."""
    for row in sorted(rows, key=lambda r: (r.get("session_id", ""), r.get("seq", 0))):
        if verify:
            ok, why = verify_segment(row)
            if not ok:
                raise ValueError(f"segment {row.get('segment_id')} failed "
                                 f"verification: {why}")
        for frame in decode_segment(row["payload"]):
            yield frame


def events_from_frames(frames: Iterable[Dict[str, Any]],
                       symbol: str) -> Iterator[Event]:
    """Frames -> the event stream a runner consumes, in RECEIVE order.

    Receive order, not exchange order: the process cannot act on what it
    has not been told. The book is reconstructed here so a strategy never
    sees a delta, only the state after it."""
    symbol = symbol.upper()
    book = BookReconstructor()
    buffered: List[Event] = []
    for frame in frames:
        topic = frame.get("topic") or ""
        if not topic.upper().endswith(symbol):
            continue
        head = topic.split(".", 1)[0]
        recv = int(frame.get("r") or 0)
        exchange = int(frame.get("ts") or 0)

        if head == "orderbook":
            state = book.apply(frame)
            if state is not None:
                buffered.append(Event(recv, exchange, "book", state))
        elif head == "publicTrade":
            for row in (frame.get("data") or []):
                buffered.append(Event(
                    int(row.get("T") or recv), int(row.get("T") or exchange), "trade",
                    Trade(exchange_ms=int(row.get("T") or exchange), recv_ms=recv,
                          price=float(row.get("p") or 0.0),
                          size=float(row.get("v") or 0.0),
                          side=str(row.get("S") or ""),
                          trade_id=str(row.get("i") or ""))))
        elif head == "allLiquidation":
            for row in (frame.get("data") or []):
                notional = float(row.get("v") or 0.0) * float(row.get("p") or 0.0)
                buffered.append(Event(recv, exchange, "liquidation",
                                      (notional, str(row.get("S") or ""))))
        elif head == "tickers":
            buffered.append(Event(recv, exchange, "ticker", frame.get("data") or {}))

    buffered.sort(key=lambda e: (e.recv_ms, 0 if e.kind == "book" else 1))
    for event in buffered:
        yield event


def describe(frames: Sequence[Dict[str, Any]], *, name: str, source: str,
             provenance: str, market: str = "Bybit USDT perpetual (linear)",
             segments: int = 0, dropped: int = 0) -> DatasetManifest:
    """Measure what the data IS, rather than what it is called."""
    topics = collections.Counter()
    symbols = set()
    exchange_stamps: List[int] = []
    recv_stamps: List[int] = []
    book_frames = 0
    trade_frames = 0
    depth_samples: List[int] = []
    sizes: List[str] = []
    has_recv = 0
    digest = hashlib.sha256()

    for frame in frames:
        topic = str(frame.get("topic") or "")
        head = topic.split(".", 1)[0]
        topics[head] += 1
        if "." in topic:
            symbols.add(topic.rsplit(".", 1)[-1].upper())
        ts = int(frame.get("ts") or 0)
        recv = int(frame.get("r") or 0)
        if ts:
            exchange_stamps.append(ts)
        if recv:
            recv_stamps.append(recv)
            has_recv += 1
        if head == "orderbook":
            book_frames += 1
            data = frame.get("data") or {}
            depth_samples.append(len(data.get("b") or []) + len(data.get("a") or []))
            for _, size in list(data.get("b") or [])[:5]:
                sizes.append(str(size))
        elif head == "publicTrade":
            trade_frames += 1
        digest.update(json.dumps(frame, sort_keys=True,
                                 separators=(",", ":")).encode("utf-8"))

    exchange_stamps.sort()
    gaps = [exchange_stamps[i + 1] - exchange_stamps[i]
            for i in range(len(exchange_stamps) - 1)]
    span_ms = (exchange_stamps[-1] - exchange_stamps[0]) if len(exchange_stamps) > 1 else 0

    quality: Dict[str, Any] = {
        "frames": len(frames),
        "book_frames": book_frames,
        "trade_frames": trade_frames,
        "distinct_exchange_stamps": len(set(exchange_stamps)),
        "receive_timestamps_present_pct": round(
            100.0 * has_recv / len(frames), 2) if frames else 0.0,
        "median_gap_ms": round(statistics.median(gaps), 1) if gaps else 0.0,
        "p95_gap_ms": round(_percentile(gaps, 95), 1) if gaps else 0.0,
        "max_gap_ms": max(gaps) if gaps else 0,
        "gaps_over_5s": sum(1 for g in gaps if g > 5_000),
        "gaps_over_30s": sum(1 for g in gaps if g > 30_000),
        "book_updates_per_second": round(book_frames / (span_ms / 1000.0), 3)
        if span_ms else 0.0,
        "trades_per_second": round(trade_frames / (span_ms / 1000.0), 3)
        if span_ms else 0.0,
        "median_levels_per_update": round(statistics.median(depth_samples), 1)
        if depth_samples else 0,
    }

    warnings: List[str] = []
    classification, why = classify(exchange_stamps, sizes, quality)
    warnings.extend(why)
    if quality["receive_timestamps_present_pct"] < 99.0:
        warnings.append("some frames carry no local receive timestamp, so "
                        "causality cannot be checked for them")
    if quality["gaps_over_30s"]:
        warnings.append(f"{quality['gaps_over_30s']} gaps longer than 30s - "
                        "nothing may be opened across them")

    return DatasetManifest(
        name=name, source=source, provenance=provenance,
        classification=classification, symbols=sorted(symbols), market=market,
        segments=segments, frames=len(frames), dropped=dropped,
        from_ms=exchange_stamps[0] if exchange_stamps else 0,
        to_ms=exchange_stamps[-1] if exchange_stamps else 0,
        sha256=digest.hexdigest(), topics=dict(topics), quality=quality,
        warnings=warnings)


def classify(stamps: Sequence[int], sizes: Sequence[str],
             quality: Dict[str, Any]) -> Tuple[str, List[str]]:
    """Is this a recording, or something that was made up?

    Three tells, each sufficient on its own, and all three fired on the
    file this project had been calling a capture:

      A PERFECT GRID. Real exchange timestamps are irregular. If every
      stamp is a multiple of the same round number, a generator produced
      them.
      A ROUND START. A capture beginning at exactly 1700000000000 began
      at a number somebody typed.
      NO SIZE ENTROPY. A real book has many distinct sizes. A handful of
      repeated values across thousands of levels is a fixture.
    """
    reasons: List[str] = []
    if len(stamps) < 10:
        return SUSPECT, ["too few timestamps to classify"]

    deltas = [stamps[i + 1] - stamps[i] for i in range(len(stamps) - 1)
              if stamps[i + 1] != stamps[i]]
    if deltas:
        step = min(deltas)
        if step > 0 and all(d % step == 0 for d in deltas) and len(set(deltas)) <= 3:
            reasons.append(f"every timestamp falls on an exact {step}ms grid - "
                           "real exchange stamps are irregular")

    if stamps[0] % 1_000_000 == 0:
        reasons.append(f"the capture begins at exactly {stamps[0]}, a number "
                       "somebody typed rather than a moment that happened")

    if sizes:
        distinct = len(set(sizes))
        if distinct <= max(3, len(sizes) // 500):
            reasons.append(f"only {distinct} distinct sizes across {len(sizes)} "
                           "book levels - a real book does not repeat like that")

    if reasons:
        return SYNTHETIC, reasons
    return REAL_CAPTURE, []


def _percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(pct / 100.0 * (len(ordered) - 1))))
    return ordered[index]
