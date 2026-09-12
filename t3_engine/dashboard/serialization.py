"""JSON serialization helpers bridging engine dataclasses/enums to the
plain dicts the dashboard frontend and REST layer consume."""

from __future__ import annotations

import dataclasses
from typing import Any, Dict

from t3_engine.common.models import Candle, Position, Scenario, Signal, Wave
from t3_engine.market_structure.structure import StructureEvent


def _enum_value(x: Any) -> Any:
    return x.value if hasattr(x, "value") else x


def candle_to_dict(c: Candle) -> Dict:
    return {
        "time": c.open_time // 1000,  # lightweight-charts wants seconds
        "open": c.open, "high": c.high, "low": c.low, "close": c.close,
        "volume": c.volume,
    }


def wave_to_dict(w: Wave) -> Dict:
    return {
        "wave_id": w.wave_id, "parent_wave_id": w.parent_wave_id, "degree": _enum_value(w.degree),
        "label": _enum_value(w.label), "direction": _enum_value(w.direction),
        "start_time": w.start_timestamp // 1000, "end_time": w.end_timestamp // 1000,
        "start_price": w.start_price, "end_price": w.end_price,
        "high": w.high, "low": w.low, "status": _enum_value(w.status),
        "confidence": w.confidence, "invalid_level": w.invalid_level,
    }


def scenario_to_dict(s: Scenario) -> Dict:
    return {
        "scenario_id": s.scenario_id, "degree": _enum_value(s.degree),
        "probability": s.probability, "status": _enum_value(s.status),
        "invalidation": s.invalidation,
        "elliott_validity": s.elliott_validity, "fib_score": s.fib_score,
        "waves": [wave_to_dict(w) for w in s.waves],
        "next_expected_label": _enum_value(s.next_expected_label) if s.next_expected_label else None,
    }


def signal_to_dict(sig: Signal) -> Dict:
    d = dataclasses.asdict(sig)
    for key in ("side", "wave_label", "entry_stage"):
        d[key] = _enum_value(getattr(sig, key))
    d["created_at_sec"] = sig.created_at // 1000
    return d


def position_to_dict(p: Position) -> Dict:
    return {
        "position_id": p.position_id, "symbol": p.symbol, "side": _enum_value(p.side),
        "entry_price": p.entry_price, "quantity": p.initial_quantity, "stop_loss": p.stop_loss,
        "wave_label": _enum_value(p.wave_label), "opened_at": p.opened_at // 1000,
        "closed_at": (p.closed_at // 1000) if p.closed_at else None,
        "realized_pnl": p.realized_pnl, "mae": p.mae, "mfe": p.mfe, "closed": p.closed,
    }


def structure_event_to_dict(e: StructureEvent) -> Dict:
    return {
        "kind": e.kind, "direction": _enum_value(e.direction),
        "time": e.breaking_timestamp // 1000, "price": e.breaking_price,
    }
