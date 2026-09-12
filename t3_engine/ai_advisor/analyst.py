"""The AI analyst: a Gemini agent that labels a chart from scratch.

This is a different thing from `request_wave_count` in advisor.py, and the
difference matters:

  request_wave_count is ONE SHOT. It gets a pre-computed pivot list in the
  prompt and returns a single count. If it misjudges the degree, or breaks
  the overlap rule, nobody finds out until the server rejects the whole
  answer and the user sees "rejected" with nothing on the chart.

  run_analyst is an AGENT LOOP. It starts with a clean chart and no pivots
  at all, and works the way an analyst works: look at the skeleton at a
  coarse degree, measure the legs, test a count against the rule engine,
  fix what broke, drop to a finer degree for the subwaves, and only then
  answer. Each of those steps is a real function call executed server-side
  (ai_advisor/analyst_tools.py), so the model reasons over data this server
  computed rather than over anything it remembered or invented.

The trust boundary has not moved. Every structure the agent submits is
re-validated by elliott_engine/external_count.py before it leaves this
module - the loop gives the model better information and more chances to
correct itself, not more authority. `submit_count` re-checks even the
counts `check_count` already approved, because nothing forces a model to
submit the thing it tested.

What the agent is NOT allowed to do: place trades, change engine settings,
read or write any state, or reach the network. Its entire world is the
six read-only functions in analyst_tools.py over one candle series.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from t3_engine.ai_advisor.advisor import DEFAULT_MODEL, AIAdvisorError, _post
from t3_engine.ai_advisor.analyst_tools import (
    FUNCTION_DECLARATIONS,
    AnalystToolbox,
    ToolCallRecord,
)
from t3_engine.ai_advisor.playbook import ELLIOTT_PLAYBOOK
from t3_engine.common.models import Candle
from t3_engine.common.types import Timeframe

DEFAULT_MAX_STEPS = 14
MAX_MAX_STEPS = 30
# Generous on purpose. The failure this replaces was a count truncated
# mid-JSON because 2048 tokens covered the model's thinking but not its
# answer; a labelling run that reasons across a whole history needs room.
ANALYST_MAX_OUTPUT_TOKENS = 8192


@dataclass
class AnalystResult:
    accepted: List[Dict[str, Any]] = field(default_factory=list)
    rejected: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    reasoning: str = ""
    steps: List[ToolCallRecord] = field(default_factory=list)
    model: str = ""
    steps_used: int = 0
    finished: bool = False
    note: str = ""

    @property
    def waves(self) -> List[Dict[str, Any]]:
        """Every accepted wave, flattened, tagged with the structure it
        belongs to so the chart can colour and group them."""
        flat = []
        for structure in self.accepted:
            for wave in structure.get("waves", []):
                flat.append({**wave,
                             "structure": structure.get("structure"),
                             "deviation_pct": structure.get("deviation_pct"),
                             "note": structure.get("note", "")})
        return flat


def opening_brief(candles: List[Candle], symbol: str, degree: Timeframe) -> str:
    """What the analyst is told before it has called anything. Deliberately
    thin: the range and the size of the job, no pivots and no hints about
    where waves might be. Finding those is the task."""
    first, last = candles[0], candles[-1]
    highest = max(c.high for c in candles)
    lowest = min(c.low for c in candles)
    return (
        f"Chart: {symbol or 'unnamed instrument'}, {degree.value} candles.\n"
        f"{len(candles)} candles, indices 0 to {len(candles) - 1}.\n"
        f"First candle opens at {first.open:g}; last candle closes at {last.close:g}.\n"
        f"Highest high {highest:g}, lowest low {lowest:g}.\n\n"
        "No pivots have been computed for you and nothing is labelled. Start by calling "
        "list_pivots at a coarse deviation to see the skeleton, then work down. "
        "Finish by calling submit_count exactly once."
    )


def _tool_payload(system_prompt: str, contents: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "tools": [{"functionDeclarations": FUNCTION_DECLARATIONS}],
        "generationConfig": {
            "temperature": 0.15,   # labelling is analysis, not invention
            "maxOutputTokens": ANALYST_MAX_OUTPUT_TOKENS,
        },
    }


def _parts_of(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The model's turn, or a clear error. A turn with no parts is almost
    always a truncation or a block, and both need to say so out loud - an
    agent loop that silently treats "no parts" as "nothing to do" spins
    until it runs out of steps and then reports success with no answer."""
    candidates = data.get("candidates") or []
    if not candidates:
        blocked = (data.get("promptFeedback") or {}).get("blockReason")
        if blocked:
            raise AIAdvisorError(f"Gemini refused to answer (blockReason: {blocked})")
        raise AIAdvisorError(f"Unexpected Gemini response shape: {json.dumps(data)[:300]}")
    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    if not parts:
        reason = candidate.get("finishReason", "unknown")
        if reason == "MAX_TOKENS":
            raise AIAdvisorError(
                "Gemini hit its output-token limit before producing an answer. Try a smaller "
                "history, or a model with a larger output budget."
            )
        raise AIAdvisorError(f"Gemini returned an empty turn (finishReason: {reason})")
    return parts


def _text_of(parts: List[Dict[str, Any]]) -> str:
    return "".join(p.get("text", "") for p in parts).strip()


def run_analyst(api_key: str, candles: List[Candle], degree: Timeframe, symbol: str = "",
                model: str = DEFAULT_MODEL, max_steps: int = DEFAULT_MAX_STEPS,
                client: Optional[httpx.Client] = None, timeout: float = 120.0) -> AnalystResult:
    """Run the label-from-scratch loop and return whatever survived
    validation. Raises AIAdvisorError only for transport/API failures - a
    model that produces a bad count is a RESULT (with the broken rules
    attached), not an exception."""
    max_steps = max(1, min(int(max_steps), MAX_MAX_STEPS))
    toolbox = AnalystToolbox(candles, degree, symbol)
    contents: List[Dict[str, Any]] = [
        {"role": "user", "parts": [{"text": opening_brief(candles, symbol, degree)}]}
    ]

    note = ""
    steps_used = 0
    for step in range(max_steps):
        steps_used = step + 1
        data = _post(api_key, model, _tool_payload(ELLIOTT_PLAYBOOK, contents), client, timeout)
        parts = _parts_of(data)
        calls = [p["functionCall"] for p in parts if isinstance(p, dict) and "functionCall" in p]

        if not calls:
            # The model answered in prose. If it already submitted, fine.
            # If not, it stopped early - say so rather than presenting an
            # empty result as a finished analysis.
            if toolbox.submitted is None:
                note = ("The model stopped without calling submit_count. Its last message: "
                        + (_text_of(parts)[:600] or "(no text)"))
            break

        contents.append({"role": "model", "parts": parts})
        responses = []
        for call in calls:
            result = toolbox.call(call.get("name", ""), call.get("args") or {})
            responses.append({"functionResponse": {"name": call.get("name", ""), "response": result}})
        contents.append({"role": "user", "parts": responses})

        if toolbox.submitted is not None:
            break
    else:
        if toolbox.submitted is None:
            note = (f"The model used all {max_steps} steps without submitting a count. "
                    "Raise the step budget or load a shorter history.")

    submitted = toolbox.submitted or {}
    return AnalystResult(
        accepted=submitted.get("accepted", []),
        rejected=submitted.get("rejected", []),
        summary=submitted.get("summary", ""),
        reasoning=submitted.get("reasoning", ""),
        steps=toolbox.calls,
        model=model,
        steps_used=steps_used,
        finished=toolbox.submitted is not None,
        note=note,
    )
