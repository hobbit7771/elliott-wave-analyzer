"""The tools the AI analyst is allowed to use, and the server code behind them.

The analyst (ai_advisor/analyst.py) does not get the chart as a picture and
it does not get a dump of ten thousand candles. It gets a set of FUNCTIONS,
executed here, on this server, against the same candle series the dashboard
is drawing. That has three consequences worth stating plainly:

  * Everything the model "sees" is something this file computed. It cannot
    hallucinate a pivot into existence, because the only pivots that exist
    are the ones list_pivots() returned from the same deterministic ZigZag
    detector the deterministic engine uses.

  * It can work at more than one degree. Elliott analysis is inherently
    multi-scale: you find the skeleton at a coarse deviation and the
    subwaves at a fine one. `deviation_pct` is a parameter on every tool,
    and pivot indices are ALWAYS relative to the deviation they came from -
    which is why check_count and submit_count take the deviation too, and
    re-derive the exact same pivot list before validating anything.

  * It can be told it is wrong before it answers. check_count runs the real
    validator (elliott_engine/external_count.py) and hands back the rule
    that broke. An analyst that can test its own count is a different thing
    from one that produces a single unchecked guess.

Nothing here mutates state, hits the network, or touches the trading path.
The worst a confused model can do with these tools is waste its own steps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from t3_engine.common.models import Candle
from t3_engine.common.types import Direction, Timeframe
from t3_engine.elliott_engine.external_count import (
    ALL_STRUCTURES,
    ExternalCountRejected,
    validate_external_structure,
)
from t3_engine.market_structure.pivots import ZigZagPivotDetector

# Guard rails on tool OUTPUT size. A 10k-candle history at a 0.2% deviation
# produces thousands of pivots; dumping those into the context would crowd
# out the reasoning that is the whole point of the exercise.
MAX_PIVOTS_RETURNED = 400
MAX_CANDLES_RETURNED = 300
MIN_DEVIATION_PCT = 0.05
MAX_DEVIATION_PCT = 25.0

FIB_RETRACEMENTS = (0.236, 0.382, 0.5, 0.618, 0.786)
FIB_EXTENSIONS = (1.0, 1.272, 1.618, 2.0, 2.618)


class ToolError(Exception):
    """A tool was called with arguments that make no sense. This is
    returned TO THE MODEL as the function result, not raised to the user:
    being told "pivot 900 does not exist, there are 240" is how it
    corrects itself on the next step."""


@dataclass
class ToolCallRecord:
    """One step of the analyst's work, kept so the UI can show what it
    actually did rather than only what it concluded."""
    name: str
    args: Dict[str, Any]
    result_summary: str


def _clamp_deviation(value: Any) -> float:
    try:
        deviation = float(value)
    except (TypeError, ValueError):
        raise ToolError(f"deviation_pct must be a number, got {value!r}")
    if not MIN_DEVIATION_PCT <= deviation <= MAX_DEVIATION_PCT:
        raise ToolError(
            f"deviation_pct {deviation} is outside the usable range "
            f"{MIN_DEVIATION_PCT}-{MAX_DEVIATION_PCT}"
        )
    return deviation


class AnalystToolbox:
    """Binds the tool implementations to one candle series.

    Pivot lists are cached per deviation because they are a pure function
    of (candles, deviation) - which is also what makes an index handed back
    by list_pivots still mean the same thing when it comes back in
    check_count three steps later."""

    def __init__(self, candles: List[Candle], degree: Timeframe, symbol: str = ""):
        if len(candles) < 10:
            raise ToolError(f"Need at least 10 candles to analyse, got {len(candles)}")
        self.candles = candles
        self.degree = degree
        self.symbol = symbol
        self._pivot_cache: Dict[float, List] = {}
        self.calls: List[ToolCallRecord] = []
        self.submitted: Optional[Dict[str, Any]] = None

    # ---- shared ----

    def pivots_at(self, deviation_pct: float) -> List:
        deviation = round(_clamp_deviation(deviation_pct), 3)
        if deviation not in self._pivot_cache:
            detector = ZigZagPivotDetector(deviation_pct=deviation)
            for index, candle in enumerate(self.candles):
                detector.update(index, candle)
            self._pivot_cache[deviation] = detector.pivots
        return self._pivot_cache[deviation]

    def _pivot_or_error(self, pivots: List, index: Any, field: str):
        if isinstance(index, bool) or not isinstance(index, (int, float)) or int(index) != index:
            raise ToolError(f"{field} must be a whole pivot index, got {index!r}")
        index = int(index)
        if not 0 <= index < len(pivots):
            raise ToolError(
                f"{field}={index} does not exist; this deviation has {len(pivots)} pivots "
                f"(valid indices 0..{len(pivots) - 1})"
            )
        return pivots[index]

    # ---- tools ----

    def list_pivots(self, deviation_pct: float = 1.0) -> Dict[str, Any]:
        """The swing skeleton at one degree of detail."""
        pivots = self.pivots_at(deviation_pct)
        truncated = len(pivots) > MAX_PIVOTS_RETURNED
        shown = pivots[-MAX_PIVOTS_RETURNED:] if truncated else pivots
        offset = len(pivots) - len(shown)
        return {
            "deviation_pct": round(_clamp_deviation(deviation_pct), 3),
            "pivot_count": len(pivots),
            "note": (
                f"Only the most recent {len(shown)} pivots are listed; indices are still absolute "
                f"(this list starts at index {offset}). Use a LARGER deviation_pct to see the whole "
                "history at a higher degree." if truncated else
                "Indices here are relative to this deviation_pct. Pass the same deviation_pct to "
                "check_count and submit_count."
            ),
            "pivots": [
                {"index": offset + i, "time": p.timestamp // 1000, "price": round(p.price, 8), "kind": p.kind}
                for i, p in enumerate(shown)
            ],
        }

    def get_candles(self, start_index: int = 0, end_index: int = -1) -> Dict[str, Any]:
        """Raw OHLC for a slice, downsampled if the slice is long. Use this
        to see the shape inside a leg, not to count pivots by eye."""
        total = len(self.candles)
        start = 0 if start_index in (None, "") else int(start_index)
        end = total - 1 if end_index in (None, "", -1) else int(end_index)
        if not 0 <= start < total:
            raise ToolError(f"start_index={start} is outside 0..{total - 1}")
        if not 0 <= end < total:
            raise ToolError(f"end_index={end} is outside 0..{total - 1}")
        if start >= end:
            raise ToolError(f"start_index={start} must be before end_index={end}")

        window = self.candles[start:end + 1]
        # Ceiling division, not floor: floor leaves a step that still yields
        # slightly MORE than the cap (910 candles / 300 -> step 3 -> 304 rows).
        step = max(1, -(-len(window) // MAX_CANDLES_RETURNED))
        sampled = window[::step][:MAX_CANDLES_RETURNED]
        return {
            "start_index": start,
            "end_index": end,
            "candles_in_range": len(window),
            "sampling": f"every {step} candle(s)" if step > 1 else "every candle",
            "candles": [
                {"i": start + i * step, "t": c.open_time // 1000, "o": c.open,
                 "h": c.high, "l": c.low, "c": c.close}
                for i, c in enumerate(sampled)
            ],
        }

    def measure_move(self, deviation_pct: float, from_pivot_index: int,
                     to_pivot_index: int) -> Dict[str, Any]:
        """Price and time distance between two pivots - the raw material
        for every ratio guideline."""
        pivots = self.pivots_at(deviation_pct)
        start = self._pivot_or_error(pivots, from_pivot_index, "from_pivot_index")
        end = self._pivot_or_error(pivots, to_pivot_index, "to_pivot_index")
        change = end.price - start.price
        return {
            "from_pivot_index": int(from_pivot_index),
            "to_pivot_index": int(to_pivot_index),
            "start_price": start.price,
            "end_price": end.price,
            "price_change": round(change, 8),
            "length": round(abs(change), 8),
            "percent_change": round(change / start.price * 100, 4) if start.price else None,
            "direction": "UP" if change > 0 else "DOWN",
            "bars": end.index - start.index,
        }

    def fibonacci_levels(self, deviation_pct: float, start_pivot_index: int,
                         end_pivot_index: int) -> Dict[str, Any]:
        """Retracements of, and extensions beyond, one measured leg."""
        pivots = self.pivots_at(deviation_pct)
        start = self._pivot_or_error(pivots, start_pivot_index, "start_pivot_index")
        end = self._pivot_or_error(pivots, end_pivot_index, "end_pivot_index")
        span = end.price - start.price
        if span == 0:
            raise ToolError("That leg has zero price range - no ratios to compute")
        return {
            "leg": f"pivot {int(start_pivot_index)} ({start.price:g}) -> {int(end_pivot_index)} ({end.price:g})",
            "length": round(abs(span), 8),
            "retracements": {f"{r:.3f}": round(end.price - span * r, 8) for r in FIB_RETRACEMENTS},
            "extensions": {f"{e:.3f}": round(start.price + span * e, 8) for e in FIB_EXTENSIONS},
        }

    def check_count(self, deviation_pct: float, structure: str, waves: List[Dict[str, Any]],
                    direction: Optional[str] = None) -> Dict[str, Any]:
        """Run the REAL validator. This is the same code path that guards
        submit_count, so a count that passes here will be accepted, and one
        that fails here will be rejected no matter how it is worded."""
        result = self._validate(deviation_pct, structure, waves, direction)
        return result

    def submit_count(self, structures: List[Dict[str, Any]], reasoning: str = "",
                     summary: str = "") -> Dict[str, Any]:
        """The final answer. Every structure is re-validated here - passing
        check_count earlier is not taken on trust, because nothing stops a
        model from submitting something other than what it checked."""
        if not isinstance(structures, list) or not structures:
            raise ToolError("submit_count needs a non-empty list of structures")
        if len(structures) > 12:
            raise ToolError(f"{len(structures)} structures is more than this chart can carry; submit at most 12")

        accepted, rejected = [], []
        for position, proposed in enumerate(structures):
            if not isinstance(proposed, dict):
                rejected.append({"position": position, "reason": f"structure #{position + 1} is not an object"})
                continue
            verdict = self._validate(
                proposed.get("deviation_pct", 1.0),
                proposed.get("structure", ""),
                proposed.get("waves", []),
                proposed.get("direction"),
            )
            entry = {
                "position": position,
                "structure": proposed.get("structure"),
                "deviation_pct": proposed.get("deviation_pct", 1.0),
                "note": str(proposed.get("note", ""))[:400],
                **verdict,
            }
            (accepted if verdict.get("valid") else rejected).append(entry)

        self.submitted = {
            "reasoning": str(reasoning)[:4000],
            "summary": str(summary)[:1000],
            "accepted": accepted,
            "rejected": rejected,
        }
        return {
            "accepted_structures": len(accepted),
            "rejected_structures": len(rejected),
            "rejections": [{"position": r["position"], "reason": r.get("reason") or r.get("notes")}
                           for r in rejected],
            "status": "Answer recorded. Analysis complete." if accepted else
                      "Nothing was accepted - every structure broke a rule. Fix them and submit again.",
        }

    # ---- validation shared by check_count and submit_count ----

    def _validate(self, deviation_pct: Any, structure: Any, waves: Any,
                  direction: Any) -> Dict[str, Any]:
        structure = str(structure or "").strip().upper()
        if structure not in ALL_STRUCTURES:
            return {"valid": False, "reason": f"Unknown structure {structure!r}; expected one of "
                                              f"{', '.join(ALL_STRUCTURES)}"}
        try:
            pivots = self.pivots_at(deviation_pct)
        except ToolError as exc:
            return {"valid": False, "reason": str(exc)}

        parsed_direction = None
        if direction:
            try:
                parsed_direction = Direction(str(direction).strip().upper())
            except ValueError:
                return {"valid": False, "reason": f"direction must be UP or DOWN, got {direction!r}"}

        try:
            validated = validate_external_structure(
                pivots, waves, structure, self.degree, direction=parsed_direction,
            )
        except ExternalCountRejected as exc:
            return {"valid": False, "reason": str(exc)}

        return {
            "valid": validated.valid,
            "broken_rule": validated.broken_rule,
            "reason": validated.notes,
            "waves": [
                {"label": w.label.value, "start_time": w.start_timestamp // 1000,
                 "end_time": w.end_timestamp // 1000, "start_price": w.start_price,
                 "end_price": w.end_price, "direction": w.direction.value}
                for w in validated.waves
            ],
            "pivot_indices": validated.pivot_indices,
        }

    # ---- dispatch ----

    def call(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Execute one model-requested tool call. Tool errors come back as
        an `error` field rather than an exception: the model is supposed to
        read them and try again."""
        handlers = {
            "list_pivots": self.list_pivots,
            "get_candles": self.get_candles,
            "measure_move": self.measure_move,
            "fibonacci_levels": self.fibonacci_levels,
            "check_count": self.check_count,
            "submit_count": self.submit_count,
        }
        handler = handlers.get(name)
        if handler is None:
            result = {"error": f"No such tool {name!r}. Available: {', '.join(handlers)}"}
        else:
            try:
                result = handler(**(args or {}))
            except ToolError as exc:
                result = {"error": str(exc)}
            except TypeError as exc:
                result = {"error": f"Bad arguments for {name}: {exc}"}
        self.calls.append(ToolCallRecord(name=name, args=args or {},
                                         result_summary=_summarize(name, result)))
        return result


def _summarize(name: str, result: Dict[str, Any]) -> str:
    """One line per step for the UI's transcript - the point is for a human
    to be able to audit the analyst's reasoning path, not to re-read its
    raw JSON."""
    if "error" in result:
        return f"error: {result['error']}"
    if name == "list_pivots":
        return f"{result.get('pivot_count')} pivots at {result.get('deviation_pct')}% deviation"
    if name == "get_candles":
        return f"{result.get('candles_in_range')} candles, {result.get('sampling')}"
    if name == "measure_move":
        return f"{result.get('direction')} {result.get('length')} over {result.get('bars')} bars"
    if name == "fibonacci_levels":
        return f"levels for {result.get('leg')}"
    if name == "check_count":
        return "valid" if result.get("valid") else f"rejected: {result.get('broken_rule') or result.get('reason')}"
    if name == "submit_count":
        return (f"{result.get('accepted_structures')} accepted, "
                f"{result.get('rejected_structures')} rejected")
    return "ok"


def _structure_enum_description() -> str:
    return ", ".join(ALL_STRUCTURES)


# Gemini function declarations (an OpenAPI-schema subset). Descriptions are
# written FOR the model - each one says not just what the tool does but when
# it is the right tool, because a tool the model misuses is worse than one
# it does not have.
FUNCTION_DECLARATIONS: List[Dict[str, Any]] = [
    {
        "name": "list_pivots",
        "description": (
            "List the confirmed swing highs and lows of the whole history at a chosen level of "
            "detail. deviation_pct is the minimum % reversal needed to confirm a swing: LARGE "
            "values (2-10) give the higher-degree skeleton, SMALL values (0.1-0.5) expose "
            "subwaves. Always start large to find the primary structure, then go smaller to "
            "subdivide. Pivot indices are relative to the deviation_pct you asked for."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "deviation_pct": {"type": "number",
                                  "description": "Minimum reversal % to confirm a swing (0.05-25)."},
            },
            "required": ["deviation_pct"],
        },
    },
    {
        "name": "get_candles",
        "description": (
            "Raw OHLC candles for a range of candle indices, downsampled if the range is long. "
            "Use it to inspect the internal shape of a leg (is it sharp or sideways? does it "
            "gap?), not to count swings - list_pivots does that better."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "start_index": {"type": "integer", "description": "First candle index."},
                "end_index": {"type": "integer", "description": "Last candle index."},
            },
            "required": ["start_index", "end_index"],
        },
    },
    {
        "name": "measure_move",
        "description": (
            "Exact price distance, percentage move and bar count between two pivots. Use this "
            "instead of estimating lengths when you check 'wave 3 is not the shortest', "
            "equality, or any ratio guideline."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "deviation_pct": {"type": "number", "description": "Which pivot set the indices belong to."},
                "from_pivot_index": {"type": "integer"},
                "to_pivot_index": {"type": "integer"},
            },
            "required": ["deviation_pct", "from_pivot_index", "to_pivot_index"],
        },
    },
    {
        "name": "fibonacci_levels",
        "description": (
            "Fibonacci retracements (0.236-0.786) and extensions (1.0-2.618) of the leg between "
            "two pivots. Use it to test where a correction actually ended against where the "
            "guidelines say it should, and to project a target for an unfinished wave."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "deviation_pct": {"type": "number"},
                "start_pivot_index": {"type": "integer"},
                "end_pivot_index": {"type": "integer"},
            },
            "required": ["deviation_pct", "start_pivot_index", "end_pivot_index"],
        },
    },
    {
        "name": "check_count",
        "description": (
            "Test a candidate count against the server's real Elliott rule engine BEFORE "
            "committing to it. Returns valid=true, or the exact rule it broke. Use it freely - "
            "it is far better to be told wave 4 overlaps wave 1 now than to submit it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "deviation_pct": {"type": "number",
                                  "description": "The pivot set these indices came from."},
                "structure": {"type": "string",
                              "description": f"One of: {_structure_enum_description()}."},
                "direction": {"type": "string",
                              "description": "UP or DOWN. Required for motive structures "
                                             "(IMPULSE, DIAGONAL_*); ignored for corrections, "
                                             "whose direction is read off wave A."},
                "waves": {
                    "type": "array",
                    "description": "The legs in canonical order: 1,2,3,4,5 for a motive count; "
                                   "A,B,C for a zigzag or flat; A,B,C,D,E for a triangle. Each "
                                   "leg must start exactly where the previous one ended.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "start_pivot_index": {"type": "integer"},
                            "end_pivot_index": {"type": "integer"},
                        },
                        "required": ["label", "start_pivot_index", "end_pivot_index"],
                    },
                },
            },
            "required": ["deviation_pct", "structure", "waves"],
        },
    },
    {
        "name": "submit_count",
        "description": (
            "Your final answer: every structure you are confident in, oldest first. Call it ONCE, "
            "after you have checked your counts. Structures may be at different deviation_pct "
            "values - that is how you express a higher-degree count plus the subwaves inside it. "
            "Every structure is re-validated; anything that breaks a rule is dropped, so submit "
            "only what you have verified."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "structures": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "structure": {"type": "string",
                                          "description": f"One of: {_structure_enum_description()}."},
                            "deviation_pct": {"type": "number"},
                            "direction": {"type": "string", "description": "UP or DOWN (motive only)."},
                            "note": {"type": "string",
                                     "description": "What this structure is in context, e.g. "
                                                    "'wave 4 of the larger impulse'."},
                            "waves": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string"},
                                        "start_pivot_index": {"type": "integer"},
                                        "end_pivot_index": {"type": "integer"},
                                    },
                                    "required": ["label", "start_pivot_index", "end_pivot_index"],
                                },
                            },
                        },
                        "required": ["structure", "deviation_pct", "waves"],
                    },
                },
                "summary": {"type": "string",
                            "description": "One sentence: where the market is in the wave count now."},
                "reasoning": {"type": "string",
                              "description": "Which rules and guidelines drove this count, and what "
                                             "would invalidate it."},
            },
            "required": ["structures", "summary", "reasoning"],
        },
    },
]
