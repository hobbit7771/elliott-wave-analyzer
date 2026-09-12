"""Structured decision journal (spec section 17).

Every accept/reject decision - and the full market context it was made
with - is appended as one JSON line to `signals.jsonl`. JSON-lines (not a
single JSON array) so the file is append-only and safe to tail/stream
live, and so a crash mid-run never corrupts previously-written records.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
from typing import Any, Dict, Optional

from t3_engine.common.models import Signal


def _default(obj: Any):
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if hasattr(obj, "value"):  # Enum
        return obj.value
    return str(obj)


class DecisionLogger:
    def __init__(self, log_dir: str = "./logs"):
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        self._signals_path = os.path.join(log_dir, "signals.jsonl")
        self._events_path = os.path.join(log_dir, "events.jsonl")

    def log_signal(self, signal: Signal, *, market_context: Optional[Dict] = None) -> None:
        record = dataclasses.asdict(signal)
        record["market_context"] = market_context or {}
        record["logged_at"] = int(time.time() * 1000)
        with open(self._signals_path, "a") as f:
            f.write(json.dumps(record, default=_default) + "\n")

    def log_event(self, event_type: str, payload: Dict) -> None:
        record = {"event_type": event_type, "timestamp": int(time.time() * 1000), "payload": payload}
        with open(self._events_path, "a") as f:
            f.write(json.dumps(record, default=_default) + "\n")
