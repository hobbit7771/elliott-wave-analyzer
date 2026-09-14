"""The AI analyst: an agent that labels a chart from scratch.

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

PROVIDER NOTE: this loop needs a model that supports tool calling. Not
every model on OrcaRouter does, and one that does not will either error or
answer in prose - which comes back as `finished: False` with the model's
last message attached, rather than as an empty result dressed up as a
finished analysis.

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
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import httpx

from t3_engine.ai_advisor.advisor import (
    ANALYSIS_TEMPERATURE,
    DEFAULT_MODEL,
    DEFAULT_THINKING,
    DEFAULT_READ_TIMEOUT,
    DEFAULT_SEED,
    AIAdvisorError,
    _finish_reason,
    _message_of,
    _post,
    apply_model_options,
)
from t3_engine.ai_advisor.analyst_tools import (
    AnalystToolbox,
    ToolCallRecord,
    openai_tools,
)
from t3_engine.ai_advisor.playbook import ELLIOTT_PLAYBOOK
from t3_engine.ai_advisor.usage import UsageMeter
from t3_engine.common.models import Candle
from t3_engine.common.types import Timeframe

# Step budget, set from MEASURED runs rather than from a feeling. Nine
# saved analyses of the same charts, read out of the analysis cache:
#
#   10 steps  ->  coverage 40.8 / 89.3 / 93.9 / 98.4 / 98.9 %   (mean 84.3)
#                 accepted structures 2 / 4 / 4 / 5 / 7         (mean 4.4)
#   15-16     ->  coverage 95.3 / 97.0 / 98.1 / 99.5 %          (mean 97.5)
#                 accepted structures 9 / 10 / 10 / 12          (mean 10.3)
#
# Two things stand out. Raising 10 -> 16 roughly DOUBLED the number of
# validated structures and removed the collapse case entirely (one 10-step
# run labelled 40.8% of its chart; the worst 16-step run managed 95.3%).
# And every single 15/16-step run spent its whole budget - the model never
# stopped on its own, so 16 was still the binding constraint, not the point
# where it had nothing left to add. That is the evidence for the owner's
# read that the model had not shown its full potential.
#
# So the ceiling goes well above where runs currently stop, and the model
# is left to decide when it is done. `steps_used` keeps coming back with
# every result, so if runs start converging at 19 this can come back down
# on the same kind of evidence it went up on.
DEFAULT_MAX_STEPS = 24
MAX_MAX_STEPS = 40
# A whole-run wall clock, separate from the per-request read timeout. The
# loop makes up to max_steps sequential calls, so without this the worst
# case is steps x timeout - long enough for a proxy in front of this app to
# give up first, which loses the transcript along with the answer. Hitting
# this returns what the agent HAS done rather than nothing.
# Raised from 480s once 429s started being retried rather than fatal: a
# retry can legitimately sleep half a minute, and a budget that expires
# during a sanctioned wait would throw away a run that was about to
# continue. Raised again to 30 minutes when the step budget went to 24 at
# maximum reasoning effort: 600s would have become the real ceiling and
# quietly capped the run at whatever fitted, which is the same mistake as
# a step budget that is too low, only harder to see. Nothing waits on the
# wall clock any more - a run is a background job (ai_advisor/jobs.py), so
# the only thing a long run costs is tokens, and those are now measured.
DEFAULT_RUN_BUDGET_SECONDS = 1800.0

# Told to the model on its LAST allowed step. Without it, a step budget
# does not end a run - it interrupts one, and an agent that was still
# exploring when the budget ran out hands back nothing at all. With it, the
# budget becomes a deadline the model can plan against, which is how a
# 2-step run can still produce a (small) count instead of an empty panel.
# How many of the most recent tool results stay in the conversation in
# full. Everything older is collapsed to its one-line summary.
#
# This is the single biggest lever on how long a run takes. The whole
# conversation is resent on every step, so a `list_pivots` result sits in
# the history and is re-read by the model on every subsequent step - by
# step 8 a run was carrying tens of kilobytes of pivot lists it had already
# used, which costs latency on every call and brings a free tier's rate
# limit forward. The model can always call the tool again if it needs the
# detail back, and the summary tells it what it found.
KEEP_FULL_TOOL_RESULTS = 3

FINAL_STEP_NUDGE = (
    "This is your LAST step. Call submit_count with complete=true now, with whatever you are "
    "genuinely confident in, "
    "even if that is a single structure or a partial count, and say in `reasoning` what you did not "
    "get to check. A small verified count is worth far more than nothing. Do not call any other tool."
)
# Sent once, halfway through the budget, if nothing has been submitted
# yet. Not a demand to stop - an instruction to SAVE progress, because
# submit_count accumulates and a run that banks nothing loses everything
# to the first upstream error.
BANK_PARTIAL_NUDGE = (
    "Before you go further: call submit_count now with the structures you have already verified, "
    "even if that is one, and with complete=false. It is additive - you will keep working "
    "afterwards and can submit more. This exists because a run that has submitted nothing loses "
    "all of its work if the connection drops, and that has really happened. Bank what you have, "
    "then carry on."
)
# Generous on purpose. The failure this replaces was a count truncated
# mid-JSON because 2048 tokens covered the model's thinking but not its
# answer; a labelling run that reasons across a whole history needs room.
#
# Raised again when reasoning effort went to `max`. Thinking tokens are
# billed and counted against THIS ceiling, so at maximum effort the model
# can spend the whole budget reasoning and emit nothing - which the
# provider then reports as "empty response", and which is exactly what
# ended a nine-step run on a 15m chart after $0.61 of work. Room for the
# thinking AND the answer is the fix; the retry above is the safety net.
ANALYST_MAX_OUTPUT_TOKENS = 32768


@dataclass
class AnalystResult:
    accepted: List[Dict[str, Any]] = field(default_factory=list)
    rejected: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    reasoning: str = ""
    steps: List[ToolCallRecord] = field(default_factory=list)
    model: str = ""
    # Tokens this run actually consumed, summed over every call it made.
    # The basis for answering "what does running this cost" with a number.
    usage: Dict[str, Any] = field(default_factory=dict)
    steps_used: int = 0
    finished: bool = False
    note: str = ""
    error: str = ""
    # The conversation itself, in order: what the model said, what it was
    # thinking, what it called, and what came back. Without this the only
    # answer to "why did it stop there" is a guess.
    transcript: List[Dict[str, Any]] = field(default_factory=list)
    # What the count says comes next, computed server-side from the waves
    # already on the chart, and how much of the history got labelled.
    projection: Optional[Dict[str, Any]] = None
    coverage: Dict[str, Any] = field(default_factory=dict)

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


def opening_brief(candles: List[Candle], symbol: str, degree: Timeframe,
                  record_line: str = "") -> str:
    """What the analyst is told before it has called anything. Deliberately
    thin: the range and the size of the job, no pivots and no hints about
    where waves might be. Finding those is the task.

    `record_line` is the one exception, and it is a statement of fact, not
    a hint: what the paper trades opened from PREVIOUS counts of this same
    chart actually did (see ai_advisor/trade_journal.py). It is included
    because an analyst who never learns whether its reading paid is
    working blind. It is phrased as history and carries no instruction to
    change anything - "your last count lost, so try something else" is how
    a model is talked into fitting its answer to the last result instead
    of to the chart."""
    first, last = candles[0], candles[-1]
    highest = max(c.high for c in candles)
    lowest = min(c.low for c in candles)
    return (
        f"Chart: {symbol or 'unnamed instrument'}, {degree.value} candles.\n"
        f"{len(candles)} candles, indices 0 to {len(candles) - 1}.\n"
        f"First candle opens at {first.open:g}; last candle closes at {last.close:g}.\n"
        f"Highest high {highest:g}, lowest low {lowest:g}.\n"
        + (f"{record_line}\n" if record_line else "")
        + "\nNo pivots have been computed for you and nothing is labelled. Start by calling "
        "list_pivots at a coarse deviation to see the skeleton, then work down. "
        "Finish by calling submit_count exactly once."
    )


def _tool_payload(system_prompt: str, messages: List[Dict[str, Any]],
                  seed: Optional[int] = DEFAULT_SEED,
                  reasoning_effort: Optional[str] = None,
                  force_submit: bool = False,
                  thinking: Optional[bool] = DEFAULT_THINKING) -> Dict[str, Any]:
    """`force_submit` pins tool_choice to submit_count, which is what turns
    "ran out of steps" into "answered with what it had". Only ever used on
    the last step, and only once the model has actually looked at some
    pivots - forcing a submission from a model that has seen nothing would
    just manufacture indices for the validator to reject."""
    choice: Any = ({"type": "function", "function": {"name": "submit_count"}}
                   if force_submit else "auto")
    return apply_model_options({
        "messages": [{"role": "system", "content": system_prompt}] + messages,
        "tools": openai_tools(),
        "tool_choice": choice,
        "temperature": ANALYSIS_TEMPERATURE,   # labelling is analysis, not invention
        "max_tokens": ANALYST_MAX_OUTPUT_TOKENS,
    }, seed=seed, reasoning_effort=reasoning_effort, thinking=thinking)


def _assistant_turn(data: Dict[str, Any]) -> Dict[str, Any]:
    """The model's turn, or a clear error. A turn with neither text nor
    tool calls is almost always a truncation, and it needs to say so out
    loud - an agent loop that silently treats "nothing came back" as
    "nothing to do" spins until it runs out of steps and then reports
    success with no answer."""
    message = _message_of(data)
    has_content = bool((message.get("content") or "").strip())
    has_calls = bool(message.get("tool_calls"))
    if not has_content and not has_calls:
        if _finish_reason(data) == "length":
            raise AIAdvisorError(
                "The model hit its output-token limit before producing an answer. Try a smaller "
                "history, or a model with a larger output budget."
            )
        raise AIAdvisorError(f"The model returned an empty turn (finish_reason: "
                             f"{_finish_reason(data) or 'unknown'})")
    return message


def trim_tool_history(messages: List[Dict[str, Any]],
                      keep: int = KEEP_FULL_TOOL_RESULTS) -> None:
    """Collapse all but the newest `keep` tool results, in place.

    Only tool RESULTS are trimmed. The model's own turns, its tool calls
    and the opening brief all stay: dropping those would break the call/
    response pairing the wire format requires, and would also erase the
    reasoning the model is building on."""
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    for index in tool_indices[:-keep] if keep else tool_indices:
        message = messages[index]
        if message.get("_trimmed"):
            continue
        summary = message.get("_summary") or "(result omitted)"
        message["content"] = json.dumps({
            "summary": summary,
            "note": "Full result omitted to keep this conversation small. Call the tool again if "
                    "you need the detail.",
        })
        message["_trimmed"] = True


def _wire_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Strip the bookkeeping keys before anything goes on the wire - an API
    that validates its request shape rejects unknown fields."""
    return [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]


def _parse_tool_arguments(call: Dict[str, Any]) -> Dict[str, Any]:
    """Tool arguments arrive as a JSON STRING in this wire format, and a
    model that emits malformed JSON there is common enough that it must not
    end the run. Returning the parse error as the tool result gives the
    model the one thing that lets it recover: what it got wrong."""
    raw = (call.get("function") or {}).get("arguments")
    if isinstance(raw, dict):
        return raw
    if raw in (None, ""):
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        return {"__arguments_error__": f"arguments were not valid JSON ({exc}): {str(raw)[:200]}"}
    return parsed if isinstance(parsed, dict) else {"__arguments_error__": "arguments must be a JSON object"}


def run_analyst(api_key: str, candles: List[Candle], degree: Timeframe, symbol: str = "",
                model: str = DEFAULT_MODEL, max_steps: int = DEFAULT_MAX_STEPS,
                client: Optional[httpx.Client] = None, timeout: float = DEFAULT_READ_TIMEOUT,
                base_url: Optional[str] = None,
                run_budget_seconds: float = DEFAULT_RUN_BUDGET_SECONDS,
                seed: Optional[int] = DEFAULT_SEED,
                reasoning_effort: Optional[str] = None,
                thinking: Optional[bool] = DEFAULT_THINKING,
                on_progress: Optional[Callable[[str], None]] = None,
                record_line: str = "") -> AnalystResult:
    """Run the label-from-scratch loop and return whatever survived
    validation.

    A model that produces a bad count is a RESULT (with the broken rules
    attached), not an exception. So is a run that times out PART WAY
    through: the tool transcript up to that point is real work and real
    information ("it listed the swings, measured wave 3, and stalled"), so
    it comes back with the error attached rather than being thrown away.

    AIAdvisorError is raised only when the FIRST call fails - at that point
    there is nothing to show, and the caller needs the failure loudly
    rather than an empty result that looks like a finished analysis."""
    max_steps = max(1, min(int(max_steps), MAX_MAX_STEPS))
    toolbox = AnalystToolbox(candles, degree, symbol)
    messages: List[Dict[str, Any]] = [
        {"role": "user", "content": opening_brief(candles, symbol, degree, record_line)}
    ]

    note = ""
    error = ""
    steps_used = 0
    transcript: List[Dict[str, Any]] = []
    meter = UsageMeter()
    seen_pivots = False
    started = time.monotonic()
    # A run is minutes long and nothing about it is visible from outside
    # while it happens. `on_progress` is how a caller (the job runner)
    # reports what the agent is doing STEP BY STEP instead of leaving a
    # spinner to stand for "working" and "hung" alike.
    def report(text: str) -> None:
        if on_progress is not None:
            on_progress(text)

    report(f"Working the {degree.value} chart: {len(candles)} candles, up to {max_steps} steps.")

    for step in range(max_steps):
        if step > 0 and time.monotonic() - started > run_budget_seconds:
            note = (f"Stopped after {step} step(s): the run passed its {run_budget_seconds:.0f}s "
                    "budget. Everything the analyst did up to that point is below.")
            break

        # BANK SOMETHING EARLY. A run that has submitted nothing is worth
        # nothing the moment anything goes wrong - and something does go
        # wrong: a real 15m run spent nine paid steps, hit one upstream
        # hiccup on the tenth, and left the chart blank for $0.61. The
        # playbook already asks for early submission and the model does not
        # always comply, so it is asked directly, once, at the halfway
        # mark. submit_count is additive, so banking a partial count costs
        # nothing - the run carries on adding to it.
        # Never on the last step: that one carries FINAL_STEP_NUDGE, and
        # "bank this and carry on" next to "this is your last step" is two
        # contradictory instructions in one turn.
        if step == max_steps // 2 and step < max_steps - 1 and toolbox.submitted is None:
            messages.append({"role": "user", "content": BANK_PARTIAL_NUDGE})
            transcript.append({"role": "system", "step": step + 1, "text": BANK_PARTIAL_NUDGE})
            report(f"Step {step + 1}: asked to bank what it has so far.")

        # On the final step, stop exploring and answer. A budget that just
        # cuts the model off mid-thought produces nothing; a budget the
        # model KNOWS about produces a smaller count.
        final_step = step == max_steps - 1
        if final_step and not toolbox.finished:
            messages.append({"role": "user", "content": FINAL_STEP_NUDGE})
            transcript.append({"role": "system", "step": step + 1, "text": FINAL_STEP_NUDGE})

        steps_used = step + 1
        report(f"Step {steps_used}/{max_steps}: asking the model.")
        try:
            data = _post(api_key, model,
                         _tool_payload(ELLIOTT_PLAYBOOK, _wire_messages(messages), seed,
                                       reasoning_effort,
                                       force_submit=final_step and seen_pivots,
                                       thinking=thinking),
                         client, timeout, base_url)
            meter.add(data.get("usage"))
            message = _assistant_turn(data)
        except AIAdvisorError as exc:
            if step == 0:
                raise          # nothing done yet - fail loudly
            error = str(exc)
            note = (f"Stopped at step {steps_used}: {error} "
                    "The steps completed before that are below.")
            steps_used = step   # the failed step did no work
            break

        calls = message.get("tool_calls") or []
        transcript.append({
            "role": "assistant",
            "step": steps_used,
            "text": (message.get("content") or "").strip(),
            "reasoning": (message.get("reasoning_content") or "").strip(),
            "tool_calls": [(c.get("function") or {}).get("name", "") for c in calls],
        })

        if not calls:
            # The model answered in prose. If it already submitted, fine.
            # If not, it stopped early - say so rather than presenting an
            # empty result as a finished analysis.
            if toolbox.submitted is None:
                note = ("The model stopped without calling submit_count. Its last message: "
                        + ((message.get("content") or "").strip()[:600] or "(no text)"))
            break

        messages.append(message)
        for call in calls:
            name = (call.get("function") or {}).get("name", "")
            args = _parse_tool_arguments(call)
            if "__arguments_error__" in args:
                result = {"error": f"Bad arguments for {name}: {args['__arguments_error__']}"}
                summary = f"error: {result['error']}"
                toolbox.calls.append(ToolCallRecord(name=name, args={}, result_summary=summary))
                args = {}
            else:
                result = toolbox.call(name, args)
                summary = toolbox.calls[-1].result_summary
            if name == "list_pivots" and "error" not in result:
                seen_pivots = True
            transcript.append({"role": "tool", "step": steps_used, "name": name,
                               "args": args, "result": summary})
            report(f"Step {steps_used}: {name} -> {summary[:120]}")
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "name": name,
                "content": json.dumps(result, default=str),
                "_summary": f"{name}({', '.join(f'{k}={v}' for k, v in list(args.items())[:4])}) -> {summary}",
            })

        trim_tool_history(messages)

        if toolbox.finished:
            break
    else:
        if toolbox.submitted is None:
            note = (f"The model used all {max_steps} step(s) without submitting a count, even after "
                    "being told the last one was its last. Raise the step budget - this agent "
                    "normally needs 6 or more steps to look at the chart and verify a count before "
                    "answering.")

    submitted = toolbox.submitted or {}
    return AnalystResult(
        projection=submitted.get("projection"),
        coverage=submitted.get("coverage") or {},
        accepted=submitted.get("accepted", []),
        rejected=submitted.get("rejected", []),
        summary=submitted.get("summary", ""),
        reasoning=submitted.get("reasoning", ""),
        steps=toolbox.calls,
        model=model,
        usage=meter.as_dict(),
        steps_used=steps_used,
        finished=toolbox.submitted is not None,
        note=note,
        error=error,
        transcript=transcript,
    )
