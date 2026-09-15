"""Replay recorded Bybit frames through a fresh engine, and score it.

The reason this is its own module and not a mode of the live engine: a
backtest that shares state with the live path is a backtest that can be
contaminated by it. `Replay` builds a NEW LeadEngine with a config of its
own, feeds it recorded messages through `handle_message` - the identical
entry point a live socket uses - and never touches the running instance.

No lookahead, enforced in three places rather than asserted once:

  1. Features are computed by the same code as live, from the same
     message stream, in timestamp order. Nothing in this package reads a
     future element: every window in rolling.py filters on
     `cutoff <= stamp <= reference`, and the structure modules use closed
     candles only.
  2. The engine's clock is the RECORDED timestamp, not wall time
     (`snapshot(now=...)`), so a slow replay cannot age a window
     differently from a fast one.
  3. Outcomes are evaluated from prices STRICTLY AFTER the signal's own
     timestamp. `_outcome` starts its scan at the first price with
     `t > signal_t`, and a test asserts that shifting the future prices
     changes the score while shifting the past ones does not.

What the report contains, per the specification: precision, recall, false
positives, lead time, MFE, MAE and expected value. Each is defined in the
function that computes it, because "precision" means nothing until you
say what counted as a positive.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from t3_engine.lead_engine.bybit_ws import parse_topic
from t3_engine.lead_engine.config import LeadEngineConfig
from t3_engine.lead_engine.engine import LeadEngine
from t3_engine.lead_engine.signal_machine import (
    A_PLUS,
    HIGH_PROBABILITY,
    PRE_BREAK_LONG,
    PRE_BREAK_SHORT,
)

# The states a replay treats as a directional call. WATCH and PRE_SIGNAL
# are excluded on purpose: they claim that something is building, not that
# a level is about to go, and scoring them as calls would measure the
# wrong thing.
ACTIONABLE = (PRE_BREAK_LONG, PRE_BREAK_SHORT, HIGH_PROBABILITY, A_PLUS)

# How far ahead an outcome is measured. A pre-break warning that is right
# an hour later was not a warning about this level.
DEFAULT_HORIZON_MS = 10 * 60 * 1000

# The move, as a fraction of price, that counts as the break actually
# happening.
DEFAULT_BREAK_PCT = 0.004


@dataclass
class ReplayEvent:
    timestamp_ms: int
    topic: str
    message: Dict[str, Any]


@dataclass
class Call:
    symbol: str
    state: str
    direction: str
    timestamp_ms: int
    price: float
    level: Optional[float]
    probability: float
    resolved: Optional[bool] = None
    lead_ms: Optional[int] = None
    mfe: float = 0.0
    mae: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "state": self.state, "direction": self.direction,
            "at": self.timestamp_ms, "price": self.price, "level": self.level,
            "break_probability": round(self.probability, 2),
            "resolved": self.resolved,
            "lead_seconds": None if self.lead_ms is None else round(self.lead_ms / 1000.0, 1),
            "mfe": round(self.mfe, 6), "mae": round(self.mae, 6),
        }


@dataclass
class ReplayReport:
    symbol: str
    events: int = 0
    frames: int = 0
    calls: List[Call] = field(default_factory=list)
    actual_breaks: int = 0
    precision: float = 0.0
    recall: float = 0.0
    false_positives: int = 0
    median_lead_seconds: Optional[float] = None
    mfe: float = 0.0
    mae: float = 0.0
    expected_value: float = 0.0
    states_seen: Dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "events": self.events, "frames": self.frames,
            "calls": [c.as_dict() for c in self.calls],
            "call_count": len(self.calls),
            "actual_breaks": self.actual_breaks,
            "precision_pct": round(self.precision * 100.0, 2),
            "recall_pct": round(self.recall * 100.0, 2),
            "false_positives": self.false_positives,
            "median_lead_seconds": self.median_lead_seconds,
            "mfe": round(self.mfe, 6), "mae": round(self.mae, 6),
            "expected_value": round(self.expected_value, 6),
            "states_seen": dict(self.states_seen),
        }


def events_from_messages(messages: Iterable[Dict[str, Any]]) -> List[ReplayEvent]:
    """Recorded frames into replay events, sorted by exchange timestamp.

    Sorting here rather than trusting the recording: a capture that
    interleaves topics from several sockets can deliver them out of order,
    and replaying them out of order would produce a book that never
    existed."""
    out: List[ReplayEvent] = []
    for message in messages:
        topic = message.get("topic")
        if not topic:
            continue
        stamp = int(message.get("ts") or 0)
        if not stamp:
            data = message.get("data")
            if isinstance(data, list) and data:
                stamp = int((data[0] or {}).get("T") or 0)
        out.append(ReplayEvent(stamp, str(topic), message))
    out.sort(key=lambda e: e.timestamp_ms)
    return out


class Replay:
    """One replay run. Owns its engine; touches no live state."""

    def __init__(self, symbol: str, config: Optional[LeadEngineConfig] = None,
                 horizon_ms: int = DEFAULT_HORIZON_MS,
                 break_pct: float = DEFAULT_BREAK_PCT) -> None:
        self.symbol = symbol.upper()
        self.horizon_ms = horizon_ms
        self.break_pct = break_pct
        config = config or LeadEngineConfig()
        # A replay engine is enabled regardless of the live flag: it opens
        # no socket and polls nothing, so "enabled" here only means its
        # accessors answer.
        replay_config = LeadEngineConfig(
            enabled=True, symbols=list({self.symbol, *config.symbols}),
            ws_url=config.ws_url, rest_base=config.rest_base,
            kline_intervals=list(config.kline_intervals),
            orderbook_depth=config.orderbook_depth, weights=config.weights,
            thresholds=config.thresholds, oi_poll_seconds=config.oi_poll_seconds,
            recompute_interval_seconds=0.0,       # every frame, deterministically
        )
        self.engine = LeadEngine(replay_config)
        self.prices: List[Tuple[int, float]] = []
        self.frames = 0

    # ---- running ----

    def run(self, events: Sequence[ReplayEvent],
            on_frame: Optional[Callable[[Dict[str, Any]], None]] = None) -> ReplayReport:
        report = ReplayReport(symbol=self.symbol)
        calls: List[Call] = []
        last_state = ""
        for event in events:
            report.events += 1
            self.engine.handle_message(event.topic, event.message)
            parsed = parse_topic(event.topic)
            if parsed["symbol"] != self.symbol:
                continue

            state = self.engine.states.get(self.symbol)
            if state is None:
                continue
            # Health is asserted from the recording's own clock: a replay
            # must not be degraded merely because the capture is old.
            self._mark_fresh(state, event.timestamp_ms)
            frame = state.snapshot(force=True, now=event.timestamp_ms / 1000.0)
            self.frames += 1
            report.frames += 1
            price = float(frame.get("price") or 0.0)
            if price > 0:
                self.prices.append((event.timestamp_ms, price))

            signal = frame["signal"]
            report.states_seen[signal["state"]] = report.states_seen.get(signal["state"], 0) + 1
            if signal["state"] != last_state and signal["state"] in ACTIONABLE:
                calls.append(Call(
                    symbol=self.symbol, state=signal["state"],
                    direction=signal["direction"] or "",
                    timestamp_ms=event.timestamp_ms, price=price,
                    level=signal.get("level"),
                    probability=float(signal.get("break_probability") or 0.0),
                ))
            last_state = signal["state"]
            if on_frame is not None:
                on_frame(frame)

        report.calls = calls
        self._score(report)
        return report

    @staticmethod
    def _mark_fresh(state, timestamp_ms: int) -> None:
        state.health.ws_connected = True
        state.health.last_book_ms = max(state.health.last_book_ms, timestamp_ms)
        state.health.last_trade_ms = max(state.health.last_trade_ms, timestamp_ms)
        state.health.last_ticker_ms = max(state.health.last_ticker_ms, timestamp_ms)

    # ---- scoring ----

    def _future(self, after_ms: int) -> List[Tuple[int, float]]:
        """Prices STRICTLY after a moment, within the horizon.

        The no-lookahead boundary, in one expression. Everything scored
        below reads only what this returns."""
        return [(t, p) for t, p in self.prices
                if after_ms < t <= after_ms + self.horizon_ms]

    def _outcome(self, call: Call) -> None:
        future = self._future(call.timestamp_ms)
        if not future or call.price <= 0:
            call.resolved = None
            return
        target = call.level if call.level else call.price
        moved = False
        for stamp, price in future:
            excursion = (price - call.price) / call.price
            if call.direction == "short":
                call.mfe = max(call.mfe, -excursion)
                call.mae = max(call.mae, excursion)
                broke = price <= target * (1.0 - self.break_pct)
            else:
                call.mfe = max(call.mfe, excursion)
                call.mae = max(call.mae, -excursion)
                broke = price >= target * (1.0 + self.break_pct)
            if broke and not moved:
                moved = True
                call.lead_ms = stamp - call.timestamp_ms
        call.resolved = moved

    def _actual_breaks(self) -> int:
        """How many times a level of the size this engine warns about was
        actually taken out, whether or not the engine said so.

        Needed for recall: without a count of what HAPPENED, "we called
        nine breaks and eight worked" says nothing about the ones missed.
        Counted as non-overlapping moves of `break_pct` from a running
        anchor, which is crude and is stated as such in the docs."""
        if len(self.prices) < 2:
            return 0
        breaks = 0
        anchor = self.prices[0][1]
        for _, price in self.prices[1:]:
            if anchor <= 0:
                anchor = price
                continue
            if abs(price - anchor) / anchor >= self.break_pct:
                breaks += 1
                anchor = price
        return breaks

    def _score(self, report: ReplayReport) -> None:
        for call in report.calls:
            self._outcome(call)
        resolved = [c for c in report.calls if c.resolved is not None]
        hits = [c for c in resolved if c.resolved]
        report.false_positives = len([c for c in resolved if not c.resolved])
        report.precision = (len(hits) / len(resolved)) if resolved else 0.0
        report.actual_breaks = self._actual_breaks()
        report.recall = (min(1.0, len(hits) / report.actual_breaks)
                         if report.actual_breaks else 0.0)
        leads = [c.lead_ms for c in hits if c.lead_ms is not None]
        report.median_lead_seconds = (round(statistics.median(leads) / 1000.0, 1)
                                      if leads else None)
        report.mfe = statistics.mean([c.mfe for c in resolved]) if resolved else 0.0
        report.mae = statistics.mean([c.mae for c in resolved]) if resolved else 0.0
        # Expected value per call, in fractions of price: what the average
        # call would have returned taken to its best excursion and stopped
        # at its worst. Not a strategy result - no sizing, no costs - and
        # the documentation says so.
        if resolved:
            report.expected_value = statistics.mean(
                [(c.mfe if c.resolved else -c.mae) for c in resolved])


def run_replay(symbol: str, messages: Iterable[Dict[str, Any]],
               config: Optional[LeadEngineConfig] = None,
               horizon_ms: int = DEFAULT_HORIZON_MS,
               break_pct: float = DEFAULT_BREAK_PCT) -> Dict[str, Any]:
    """Convenience wrapper: recorded frames in, report out."""
    events = events_from_messages(messages)
    replay = Replay(symbol, config, horizon_ms, break_pct)
    report = replay.run(events)
    return {**report.as_dict(), "created_at": int(time.time() * 1000),
            "horizon_ms": horizon_ms, "break_pct": break_pct}
