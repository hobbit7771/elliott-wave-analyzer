"""The AI analyst: the toolbox it works with, and the agent loop itself.

Everything here runs offline against a mocked transport. That is not a
limitation of the tests - it is the design being checked. The analyst's
entire world is six read-only functions over one candle series, so the
interesting failures (a hallucinated pivot, a count that breaks the overlap
rule, a model that never finishes) are all reproducible without a network.
"""

import json

import httpx
import pytest

from t3_engine.ai_advisor.advisor import AIAdvisorError
from t3_engine.ai_advisor.analyst import run_analyst
from t3_engine.ai_advisor.analyst_tools import (
    FUNCTION_DECLARATIONS,
    AnalystToolbox,
    ToolError,
)
from t3_engine.backtest.synthetic_data import generate_synthetic_series
from t3_engine.common.types import Timeframe

CANDLES = generate_synthetic_series(num_cycles=2)
DEGREE = Timeframe.M5

# The synthetic fixture's first five swings form a clean impulse:
# pivot 0 LOW -> 1 HIGH (w1), 1 -> 2 (w2), 2 -> 3 (w3), 3 -> 4 (w4), 4 -> 5 (w5).
GOOD_IMPULSE = [
    {"label": label, "start_pivot_index": i, "end_pivot_index": i + 1}
    for i, label in enumerate(["1", "2", "3", "4", "5"])
]


def toolbox():
    return AnalystToolbox(CANDLES, DEGREE, "SYNTHETIC")


# ---------------------------------------------------------------- toolbox

def test_list_pivots_is_the_only_way_pivots_exist():
    """The model is never handed a pivot list in its prompt - it has to ask
    for one, and what it gets back came from the same deterministic ZigZag
    detector the trading engine uses."""
    result = toolbox().list_pivots(1.0)
    assert result["pivot_count"] > 0
    assert result["pivots"][0]["index"] == 0
    assert result["pivots"][0]["kind"] in ("HIGH", "LOW")
    assert result["deviation_pct"] == 1.0


def test_a_larger_deviation_never_shows_more_swings_than_a_smaller_one():
    """Degree control is the whole reason deviation_pct is a parameter:
    coarse for the skeleton, fine for subwaves. If this inverted, the
    'work top-down' instruction would be meaningless."""
    box = toolbox()
    fine = box.list_pivots(0.1)["pivot_count"]
    coarse = box.list_pivots(8.0)["pivot_count"]
    assert coarse <= fine


def test_pivot_indices_are_stable_across_calls_at_the_same_deviation():
    """An index handed out in step 1 must still mean the same swing when it
    comes back in check_count at step 9."""
    box = toolbox()
    first = box.list_pivots(1.0)["pivots"]
    box.list_pivots(4.0)
    again = box.list_pivots(1.0)["pivots"]
    assert first == again


def test_an_out_of_range_deviation_is_refused_rather_than_clamped_silently():
    with pytest.raises(ToolError, match="outside the usable range"):
        toolbox().list_pivots(500)


def test_get_candles_never_returns_more_rows_than_the_context_can_carry():
    """A 10k-candle history dumped into the prompt would crowd out the
    reasoning that is the whole point, so long ranges are downsampled."""
    box = AnalystToolbox(generate_synthetic_series(num_cycles=10), DEGREE)
    result = box.get_candles(0, len(box.candles) - 1)
    assert len(result["candles"]) <= 300
    assert result["candles_in_range"] == len(box.candles)
    assert "every" in result["sampling"]


def test_get_candles_rejects_a_backwards_range():
    with pytest.raises(ToolError, match="must be before"):
        toolbox().get_candles(100, 20)


def test_get_candles_rejects_an_index_past_the_end_of_the_history():
    with pytest.raises(ToolError, match="outside"):
        toolbox().get_candles(0, 99999)


def test_measure_move_reports_length_and_bars_for_the_ratio_guidelines():
    result = toolbox().measure_move(1.0, 0, 1)
    assert result["length"] > 0
    assert result["bars"] > 0
    assert result["direction"] in ("UP", "DOWN")


def test_fibonacci_levels_cover_retracements_and_extensions():
    result = toolbox().fibonacci_levels(1.0, 0, 1)
    assert "0.618" in result["retracements"]
    assert "1.618" in result["extensions"]


def test_a_hallucinated_pivot_index_comes_back_as_a_correctable_message():
    """Not an exception: the model is supposed to READ this and fix its
    next call. An error it never sees teaches it nothing."""
    result = toolbox().call("measure_move", {"deviation_pct": 1.0,
                                             "from_pivot_index": 0,
                                             "to_pivot_index": 9999})
    assert "does not exist" in result["error"]
    assert "valid indices" in result["error"]


def test_calling_a_tool_that_does_not_exist_lists_the_ones_that_do():
    result = toolbox().call("draw_trendline", {})
    assert "No such tool" in result["error"]
    assert "list_pivots" in result["error"]


# ------------------------------------------------------- count validation

def test_check_count_accepts_a_real_impulse_and_returns_server_built_prices():
    box = toolbox()
    result = box.check_count(1.0, "IMPULSE", GOOD_IMPULSE, direction="UP")
    assert result["valid"]
    assert [w["label"] for w in result["waves"]] == ["1", "2", "3", "4", "5"]
    # Prices come from the server's own pivot objects, never from anything
    # the model said - and not even from the rounded copies it was shown.
    assert result["waves"][0]["start_price"] == box.pivots_at(1.0)[0].price


def test_check_count_names_the_rule_that_broke_so_the_model_can_fix_it():
    """The point of giving the agent a validator is that being told 'wave 4
    overlaps wave 1' is actionable, while 'rejected' is not."""
    # Counting the first advance as one wave pushes wave 4 back down into
    # wave 1's price territory - legal-looking, and flatly against rule 3.
    bad = [{"label": label, "start_pivot_index": start, "end_pivot_index": end}
           for label, start, end in
           [("1", 0, 3), ("2", 3, 4), ("3", 4, 5), ("4", 5, 6), ("5", 6, 7)]]
    result = toolbox().check_count(1.0, "IMPULSE", bad, direction="UP")
    assert not result["valid"]
    assert result["broken_rule"] == "WAVE4_OVERLAPS_WAVE1"
    assert "Wave 1 territory" in result["reason"]


def test_check_count_refuses_an_unknown_structure_name():
    result = toolbox().check_count(1.0, "WYCKOFF_SPRING", GOOD_IMPULSE, direction="UP")
    assert not result["valid"]
    assert "Unknown structure" in result["reason"]


def test_submit_count_revalidates_rather_than_trusting_an_earlier_check():
    """Nothing forces a model to submit the count it tested. So submission
    re-runs the same validator, and a structure that fails is dropped into
    `rejected` with its reason rather than reaching the chart."""
    box = toolbox()
    box.check_count(1.0, "IMPULSE", GOOD_IMPULSE, direction="UP")   # passes
    result = box.submit_count(structures=[
        {"structure": "IMPULSE", "deviation_pct": 1.0, "direction": "UP", "waves": GOOD_IMPULSE},
        {"structure": "IMPULSE", "deviation_pct": 1.0, "direction": "UP",
         "waves": [{"label": "1", "start_pivot_index": 0, "end_pivot_index": 400}]},
    ], summary="s", reasoning="r")
    assert result["accepted_structures"] == 1
    assert result["rejected_structures"] == 1
    assert "does not exist" in json.dumps(result["rejections"])


def test_submit_count_with_nothing_valid_says_so_instead_of_reporting_success():
    box = toolbox()
    result = box.submit_count(structures=[
        {"structure": "IMPULSE", "deviation_pct": 1.0, "direction": "UP",
         "waves": [{"label": "1", "start_pivot_index": 5, "end_pivot_index": 2}]},
    ], summary="s", reasoning="r")
    assert result["accepted_structures"] == 0
    assert "Nothing was accepted" in result["status"]


def test_every_declared_tool_has_an_implementation_behind_it():
    """A declaration the dispatcher can't route is a tool the model will
    call and always get an error from."""
    box = toolbox()
    for declaration in FUNCTION_DECLARATIONS:
        result = box.call(declaration["name"], {})
        assert "No such tool" not in str(result.get("error", ""))


# ------------------------------------------------------------ agent loop

def _sse(*chunks):
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def function_call_turn(name, args, call_id="call_1"):
    """One assistant turn asking for a tool call, streamed - and streamed
    the way a real one arrives: the name and id in one frame, the arguments
    split across the next two."""
    encoded = json.dumps(args)
    midpoint = len(encoded) // 2
    return httpx.Response(200, text=_sse(
        {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
            {"index": 0, "id": call_id, "type": "function",
             "function": {"name": name, "arguments": encoded[:midpoint]}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": encoded[midpoint:]}}]},
            "finish_reason": "tool_calls"}]},
    ))


def text_turn(text, finish_reason="stop"):
    return httpx.Response(200, text=_sse(
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                      "finish_reason": finish_reason}]},
    ))


def scripted_client(turns):
    """Replays a fixed sequence of model turns, recording what was sent."""
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return turns[min(len(sent) - 1, len(turns) - 1)]

    return httpx.Client(transport=httpx.MockTransport(handler)), sent


def test_the_agent_works_the_chart_then_submits_a_validated_count():
    client, sent = scripted_client([
        function_call_turn("list_pivots", {"deviation_pct": 1.0}),
        function_call_turn("check_count", {"deviation_pct": 1.0, "structure": "IMPULSE",
                                           "direction": "UP", "waves": GOOD_IMPULSE}),
        function_call_turn("submit_count", {
            "structures": [{"structure": "IMPULSE", "deviation_pct": 1.0,
                            "direction": "UP", "waves": GOOD_IMPULSE,
                            "note": "the whole visible advance"}],
            "summary": "Five waves up are complete.",
            "reasoning": "Wave 3 is the longest; wave 4 stays clear of wave 1.",
            "complete": True,
        }),
    ])
    result = run_analyst("sk-test", CANDLES, DEGREE, symbol="SYNTHETIC", client=client)

    assert result.finished
    assert len(result.accepted) == 1
    assert [w["label"] for w in result.waves] == ["1", "2", "3", "4", "5"]
    assert result.summary.startswith("Five waves")
    assert [s.name for s in result.steps] == ["list_pivots", "check_count", "submit_count"]
    assert result.steps[1].result_summary == "valid"


def test_the_agent_is_given_tools_and_the_playbook_not_a_pre_made_count():
    """The brief must not leak an answer: no pivots, no labels. If it did,
    'the model found the structure itself' would stop being true."""
    client, sent = scripted_client([
        function_call_turn("submit_count", {
            "structures": [{"structure": "IMPULSE", "deviation_pct": 1.0,
                            "direction": "UP", "waves": GOOD_IMPULSE}],
            "summary": "s", "reasoning": "r", "complete": True,
        }),
    ])
    run_analyst("sk-test", CANDLES, DEGREE, symbol="SYNTHETIC", client=client)

    payload = sent[0]
    brief = payload["messages"][1]["content"]
    assert "No pivots have been computed for you" in brief
    # The brief carries the range and the size of the job, and no structure:
    # no swing list, no labels, no direction, nothing to agree with.
    for leak in ("wave 3", "impulse", "zigzag", "HIGH", "LOW", "uptrend", "downtrend"):
        assert leak not in brief, f"the opening brief leaks {leak!r}"
    assert [t["function"]["name"] for t in payload["tools"]] == [
        "list_pivots", "get_candles", "measure_move", "fibonacci_levels",
        "fibonacci_confluence", "swing_statistics", "volume_profile", "momentum",
        "check_count", "submit_count"]
    assert all(t["type"] == "function" for t in payload["tools"])
    system = payload["messages"][0]["content"]
    assert "Wave 3 is never the shortest" in system
    assert "GUIDELINES" in system


def test_tool_results_are_fed_back_so_the_model_reasons_over_real_data():
    client, sent = scripted_client([
        function_call_turn("list_pivots", {"deviation_pct": 1.0}),
        function_call_turn("submit_count", {
            "structures": [{"structure": "IMPULSE", "deviation_pct": 1.0,
                            "direction": "UP", "waves": GOOD_IMPULSE}],
            "summary": "s", "reasoning": "r", "complete": True,
        }),
    ])
    run_analyst("sk-test", CANDLES, DEGREE, client=client)

    second_request = sent[1]
    roles = [turn["role"] for turn in second_request["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    fed_back = second_request["messages"][3]
    assert fed_back["name"] == "list_pivots"
    assert fed_back["tool_call_id"] == "call_1"
    assert json.loads(fed_back["content"])["pivot_count"] > 0


def test_a_model_that_never_submits_is_reported_as_unfinished_not_as_success():
    """The quiet failure this guards against: an empty result presented as
    a completed analysis."""
    client, _ = scripted_client([text_turn("I think this is probably a wave 3 somewhere.")])
    result = run_analyst("sk-test", CANDLES, DEGREE, client=client)

    assert not result.finished
    assert result.accepted == []
    assert "without calling submit_count" in result.note
    assert "wave 3" in result.note


def test_a_model_that_loops_forever_is_stopped_by_the_step_budget():
    client, sent = scripted_client([function_call_turn("list_pivots", {"deviation_pct": 1.0})])
    result = run_analyst("sk-test", CANDLES, DEGREE, client=client, max_steps=4)

    assert not result.finished
    assert result.steps_used == 4
    assert len(sent) == 4
    assert "all 4 step(s) without submitting" in result.note
    assert "6 or more steps" in result.note      # and what to do about it


def test_the_last_step_tells_the_model_it_is_the_last_and_forces_an_answer():
    """A budget that just cuts the model off mid-thought produces nothing.
    A budget the model KNOWS about produces a smaller count - which is why
    a 2-step run can still come back with something."""
    client, sent = scripted_client([
        function_call_turn("list_pivots", {"deviation_pct": 1.0}),
        function_call_turn("submit_count", {
            "structures": [{"structure": "IMPULSE", "deviation_pct": 1.0,
                            "direction": "UP", "waves": GOOD_IMPULSE}],
            "summary": "s", "reasoning": "r"}, call_id="call_2"),
    ])
    result = run_analyst("sk-test", CANDLES, DEGREE, client=client, max_steps=2)

    assert result.finished
    assert len(result.accepted) == 1
    last_user_turn = sent[1]["messages"][-1]
    assert last_user_turn["role"] == "user"
    assert "LAST step" in last_user_turn["content"]
    # And the choice is pinned, because it HAS seen pivots by now.
    assert sent[1]["tool_choice"] == {"type": "function", "function": {"name": "submit_count"}}


def test_a_submission_is_not_forced_before_the_model_has_seen_any_pivots():
    """Forcing an answer out of a model that has looked at nothing just
    manufactures indices for the validator to throw out."""
    client, sent = scripted_client([text_turn("I have no idea where to start.")])
    run_analyst("sk-test", CANDLES, DEGREE, client=client, max_steps=1)

    assert sent[0]["tool_choice"] == "auto"


def test_a_truncated_turn_is_reported_as_a_token_budget_problem():
    """This is the production failure that motivated the budget fix: the
    answer was cut off, and the error said 'Unterminated string', which
    sends you debugging JSON instead of raising the limit."""
    empty_truncated = httpx.Response(200, text=_sse(
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                      "finish_reason": "length"}]}))
    client, _ = scripted_client([empty_truncated])

    with pytest.raises(AIAdvisorError, match="output-token limit"):
        run_analyst("sk-test", CANDLES, DEGREE, client=client)


def test_a_tool_error_does_not_end_the_run_it_is_handed_back_to_the_model():
    client, sent = scripted_client([
        function_call_turn("measure_move", {"deviation_pct": 1.0, "from_pivot_index": 0,
                                            "to_pivot_index": 9999}),
        function_call_turn("submit_count", {
            "structures": [{"structure": "IMPULSE", "deviation_pct": 1.0,
                            "direction": "UP", "waves": GOOD_IMPULSE}],
            "summary": "s", "reasoning": "r", "complete": True,
        }),
    ])
    result = run_analyst("sk-test", CANDLES, DEGREE, client=client)

    assert result.finished
    assert "error" in result.steps[0].result_summary
    handed_back = json.loads(sent[1]["messages"][3]["content"])
    assert "does not exist" in handed_back["error"]


def test_no_api_key_fails_before_anything_is_sent():
    with pytest.raises(AIAdvisorError, match="No OpenRouter API key"):
        run_analyst("", CANDLES, DEGREE)


def test_malformed_tool_arguments_do_not_end_the_run():
    """Models emit invalid JSON in the arguments string often enough that
    it must be recoverable. The parse error goes back as the tool result -
    the one thing that lets the model fix its next call."""
    broken = httpx.Response(200, text=_sse(
        {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
            {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": "list_pivots", "arguments": '{"deviation_pct": 1.0'}}]},
            "finish_reason": "tool_calls"}]}))
    client, sent = scripted_client([broken, function_call_turn("submit_count", {
        "structures": [{"structure": "IMPULSE", "deviation_pct": 1.0,
                        "direction": "UP", "waves": GOOD_IMPULSE}],
        "summary": "s", "reasoning": "r",
    }, call_id="call_2")])
    result = run_analyst("sk-test", CANDLES, DEGREE, client=client)

    assert result.finished
    assert "not valid JSON" in result.steps[0].result_summary
    assert "not valid JSON" in json.loads(sent[1]["messages"][3]["content"])["error"]


def test_a_model_with_no_tool_support_answers_in_prose_and_is_reported_as_such():
    """Not every router model supports tool calling. One that doesn't must
    not look like a finished analysis with nothing in it."""
    client, _ = scripted_client([text_turn("I cannot call functions, but here is my view: wave 3.")])
    result = run_analyst("sk-test", CANDLES, DEGREE, client=client)

    assert not result.finished
    assert result.accepted == []
    assert "cannot call functions" in result.note


def test_a_run_that_stalls_part_way_keeps_the_work_it_already_did():
    """The production failure this fixes: a read timeout mid-run threw away
    the whole run, so a user who waited three minutes got an error and no
    trace of what the agent had done. The transcript up to the stall is
    real work and real information."""
    def handler(request: httpx.Request) -> httpx.Response:
        if len(sent) == 1:
            return _first
        raise httpx.ReadTimeout("timed out", request=request)

    sent = []
    _first = function_call_turn("list_pivots", {"deviation_pct": 3.0})

    def counting_handler(request: httpx.Request) -> httpx.Response:
        sent.append(1)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(counting_handler))
    result = run_analyst("sk-test", CANDLES, DEGREE, client=client, max_steps=6)

    assert not result.finished
    assert result.steps                              # the completed step survived
    assert result.steps[0].name == "list_pivots"
    assert "did not answer" in result.error
    assert "Stopped at step" in result.note
    assert result.steps_used == 1                    # the failed step did no work


def test_a_failure_on_the_very_first_call_is_raised_not_swallowed():
    """With nothing done, an empty result would look like a finished
    analysis that found no structure. The caller needs the failure."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(AIAdvisorError, match="did not answer"):
        run_analyst("sk-test", CANDLES, DEGREE, client=client)


def test_the_whole_run_is_bounded_by_a_wall_clock_not_only_by_step_count():
    """max_steps x per-request timeout is the worst case, which is long
    enough for a proxy in front of this app to give up first - and that
    loses the transcript along with the answer."""
    client, sent = scripted_client([function_call_turn("list_pivots", {"deviation_pct": 1.0})])
    result = run_analyst("sk-test", CANDLES, DEGREE, client=client, max_steps=20,
                         run_budget_seconds=0.0)

    assert not result.finished
    assert len(sent) == 1            # one step ran, then the budget stopped it
    assert "budget" in result.note


def test_the_transcript_records_the_conversation_not_just_the_tool_calls():
    """"Why did it stop there" is unanswerable from a list of tool names.
    The transcript keeps what the model SAID and what it was thinking
    alongside what it called and what came back."""
    thinking = httpx.Response(200, text=_sse(
        {"choices": [{"index": 0, "delta": {"role": "assistant",
                                            "reasoning_content": "Coarse first, "}}]},
        {"choices": [{"index": 0, "delta": {"reasoning_content": "then subdivide.",
                                            "content": "Let me look at the skeleton."}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "c1", "type": "function",
             "function": {"name": "list_pivots", "arguments": '{"deviation_pct": 3.0}'}}]},
            "finish_reason": "tool_calls"}]},
    ))
    client, _ = scripted_client([thinking, function_call_turn("submit_count", {
        "structures": [{"structure": "IMPULSE", "deviation_pct": 1.0,
                        "direction": "UP", "waves": GOOD_IMPULSE}],
        "summary": "s", "reasoning": "r", "complete": True}, call_id="c2")])
    result = run_analyst("sk-test", CANDLES, DEGREE, client=client, max_steps=6)

    assistant_turns = [e for e in result.transcript if e["role"] == "assistant"]
    assert assistant_turns[0]["text"] == "Let me look at the skeleton."
    assert assistant_turns[0]["reasoning"] == "Coarse first, then subdivide."
    assert assistant_turns[0]["tool_calls"] == ["list_pivots"]

    tool_turns = [e for e in result.transcript if e["role"] == "tool"]
    assert tool_turns[0]["name"] == "list_pivots"
    assert tool_turns[0]["args"] == {"deviation_pct": 3.0}
    assert "pivots" in tool_turns[0]["result"]


def test_the_final_step_instruction_appears_in_the_transcript_too():
    """It changes what the model does, so hiding it would make the
    transcript misleading about why the last turn looks different."""
    client, _ = scripted_client([function_call_turn("submit_count", {
        "structures": [{"structure": "IMPULSE", "deviation_pct": 1.0,
                        "direction": "UP", "waves": GOOD_IMPULSE}],
        "summary": "s", "reasoning": "r", "complete": True})])
    result = run_analyst("sk-test", CANDLES, DEGREE, client=client, max_steps=1)

    system_turns = [e for e in result.transcript if e["role"] == "system"]
    assert system_turns and "LAST step" in system_turns[0]["text"]


def test_transcript_steps_are_numbered_so_you_can_see_where_it_ended():
    client, _ = scripted_client([function_call_turn("list_pivots", {"deviation_pct": 1.0})])
    result = run_analyst("sk-test", CANDLES, DEGREE, client=client, max_steps=3)

    steps = sorted({e["step"] for e in result.transcript})
    assert steps == [1, 2, 3]


def test_old_tool_results_are_collapsed_so_the_context_stops_growing():
    """The whole conversation is resent on every step, so a pivot list sits
    in the history and is re-read on every subsequent call. By step 8 a run
    was carrying tens of kilobytes it had already used - latency on every
    call, and a free tier's rate limit brought forward."""
    from t3_engine.ai_advisor.analyst import KEEP_FULL_TOOL_RESULTS, trim_tool_history

    messages = [{"role": "user", "content": "brief"}]
    for i in range(6):
        messages.append({"role": "assistant", "content": None, "tool_calls": [{"id": f"c{i}"}]})
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "name": "list_pivots",
                         "content": json.dumps({"pivots": list(range(500))}),
                         "_summary": f"list_pivots(deviation_pct={i}) -> 96 pivots"})
    original_size = len(messages[2]["content"])

    trim_tool_history(messages)

    tools = [m for m in messages if m["role"] == "tool"]
    kept = [m for m in tools if not m.get("_trimmed")]
    assert len(kept) == KEEP_FULL_TOOL_RESULTS      # newest survive in full
    assert kept == tools[-KEEP_FULL_TOOL_RESULTS:]
    # Each collapsed result is a fraction of what it was, and the saving
    # recurs on every later step because the whole history is resent.
    collapsed_sizes = [len(m["content"]) for m in tools if m.get("_trimmed")]
    assert collapsed_sizes and max(collapsed_sizes) < original_size / 5

    # A collapsed result still says what it found, and how to get it back.
    collapsed = json.loads(tools[0]["content"])
    assert "96 pivots" in collapsed["summary"]
    assert "Call the tool again" in collapsed["note"]


def test_trimming_never_breaks_the_call_response_pairing():
    """Dropping an assistant turn or a tool_call_id would make the request
    invalid - only the RESULT bodies are trimmed."""
    from t3_engine.ai_advisor.analyst import trim_tool_history

    messages = [{"role": "user", "content": "brief"}]
    for i in range(5):
        messages.append({"role": "assistant", "content": None, "tool_calls": [{"id": f"c{i}"}]})
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "name": "t",
                         "content": "{}", "_summary": "s"})
    roles_before = [m["role"] for m in messages]
    ids_before = [m.get("tool_call_id") for m in messages]

    trim_tool_history(messages)

    assert [m["role"] for m in messages] == roles_before
    assert [m.get("tool_call_id") for m in messages] == ids_before


def test_bookkeeping_keys_never_reach_the_wire():
    """An API that validates its request shape rejects unknown fields."""
    client, sent = scripted_client([
        function_call_turn("list_pivots", {"deviation_pct": 1.0}),
        function_call_turn("submit_count", {
            "structures": [{"structure": "IMPULSE", "deviation_pct": 1.0,
                            "direction": "UP", "waves": GOOD_IMPULSE}],
            "summary": "s", "reasoning": "r"}, call_id="c2"),
    ])
    run_analyst("sk-test", CANDLES, DEGREE, client=client, max_steps=6)

    for message in sent[1]["messages"]:
        assert not any(key.startswith("_") for key in message), message


def test_the_models_reasoning_is_carried_into_the_next_step_unmodified():
    """The agent loop is many turns long, and the provider resumes the
    model's own reasoning from reasoning_details. Dropping them - or
    rebuilding them, since some are signed blobs - makes every step start
    its thinking over: worse answers and a larger bill."""
    first = httpx.Response(200, text=_sse(
        {"choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_details": [
            {"index": 0, "type": "reasoning.encrypted", "data": "OPAQUE==", "id": "rs_1"}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "c1", "type": "function",
             "function": {"name": "list_pivots", "arguments": '{"deviation_pct": 2.0}'}}]},
            "finish_reason": "tool_calls"}]},
    ))
    client, sent = scripted_client([first, function_call_turn("submit_count", {
        "structures": [{"structure": "IMPULSE", "deviation_pct": 1.0,
                        "direction": "UP", "waves": GOOD_IMPULSE}],
        "summary": "s", "reasoning": "r", "complete": True}, call_id="c2")])
    run_analyst("sk-or-test", CANDLES, DEGREE, client=client, max_steps=6)

    assistant_turn = next(m for m in sent[1]["messages"] if m["role"] == "assistant")
    assert assistant_turn["reasoning_details"] == [
        {"index": 0, "type": "reasoning.encrypted", "data": "OPAQUE==", "id": "rs_1"}]


def test_trimming_tool_history_never_touches_the_reasoning_chain():
    """Only tool result BODIES are collapsed. Losing an assistant turn's
    reasoning_details would break the chain the provider resumes from."""
    from t3_engine.ai_advisor.analyst import trim_tool_history

    messages = [{"role": "user", "content": "brief"}]
    for i in range(6):
        messages.append({"role": "assistant", "content": None,
                         "reasoning_details": [{"index": 0, "data": f"blob-{i}"}],
                         "tool_calls": [{"id": f"c{i}"}]})
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "name": "t",
                         "content": "x" * 500, "_summary": "s"})

    trim_tool_history(messages)

    blobs = [m["reasoning_details"][0]["data"] for m in messages if m["role"] == "assistant"]
    assert blobs == [f"blob-{i}" for i in range(6)]


# ---- finishing the chart, and saying where it goes next ----

def test_a_partial_submission_is_told_what_is_still_unlabelled():
    """The complaint this answers: a chart labelled on the left third and
    bare on the right. The model had simply stopped, and nothing asked it
    to go on."""
    box = toolbox()
    result = box.submit_count(structures=[
        {"structure": "IMPULSE", "deviation_pct": 1.0, "direction": "UP", "waves": GOOD_IMPULSE},
    ], summary="s", reasoning="r")

    assert not box.finished                       # the run continues
    assert "%" in result["chart_covered"]
    assert result["unlabelled"]                   # and where the gaps are
    assert "submit_count again" in result["status"]


def test_structures_accumulate_across_several_submissions():
    """One all-or-nothing answer is what made a partial count the final
    one. Submissions add up instead."""
    box = toolbox()
    box.submit_count(structures=[
        {"structure": "IMPULSE", "deviation_pct": 1.0, "direction": "UP", "waves": GOOD_IMPULSE},
    ], summary="first", reasoning="r")
    second = box.submit_count(structures=[
        {"structure": "IMPULSE", "deviation_pct": 1.0, "direction": "UP", "waves": GOOD_IMPULSE},
    ], summary="", reasoning="", complete=True)

    assert second["accepted_structures"] == 2
    assert box.finished
    assert box.submitted["summary"] == "first"    # an empty follow-up does not erase it


def test_a_fully_covered_chart_ends_the_run_without_being_told_to():
    box = toolbox()
    whole_chart = [{"structure": "IMPULSE", "deviation_pct": 1.0, "direction": "UP",
                    "waves": GOOD_IMPULSE}]
    box.submit_count(structures=whole_chart, summary="s", reasoning="r")
    # Pretend the accepted structure spans everything.
    box.submitted["accepted"][0]["waves"][0]["start_time"] = CANDLES[0].open_time // 1000
    box.submitted["accepted"][0]["waves"][-1]["end_time"] = CANDLES[-1].open_time // 1000
    box.submit_count(structures=whole_chart, summary="s", reasoning="r")
    assert box.finished


def test_the_server_computes_the_projection_the_model_never_supplies_prices():
    """A count that stops at the last pivot answers "what happened". The
    reason to count at all is what it implies comes next - and that is
    arithmetic over waves already on the chart, not the model's opinion."""
    from t3_engine.ai_advisor.analyst_tools import project_next_wave

    waves = [
        {"label": "1", "start_price": 100.0, "end_price": 120.0, "end_time": 10},
        {"label": "2", "start_price": 120.0, "end_price": 110.0, "end_time": 20},
        {"label": "3", "start_price": 110.0, "end_price": 150.0, "end_time": 30},
        {"label": "4", "start_price": 150.0, "end_price": 138.0, "end_time": 40},
    ]
    projection = project_next_wave(waves, "5")
    assert projection["next_label"] == "5"
    assert projection["from_price"] == 138.0            # anchored on the last wave's end
    assert "wave 1 length" in projection["basis"]
    # Wave 5 targets = wave-1 length (20) projected off the end of wave 4.
    prices = [t["price"] for t in projection["targets"]]
    assert 158.0 in prices                              # 138 + 1.000 x 20


def test_a_projection_that_needs_a_wave_the_count_lacks_is_absent_not_invented():
    """An honest absence beats a number derived from a wave that is not
    there."""
    from t3_engine.ai_advisor.analyst_tools import project_next_wave

    assert project_next_wave([{"label": "1", "start_price": 1, "end_price": 2, "end_time": 1}],
                             "5") is None
    assert project_next_wave([], "3") is None
    assert project_next_wave([{"label": "1", "start_price": 1, "end_price": 2, "end_time": 1}],
                             "banana") is None


def test_the_projection_rides_back_with_the_result():
    box = toolbox()
    box.submit_count(
        structures=[{"structure": "IMPULSE", "deviation_pct": 1.0, "direction": "UP",
                     "waves": GOOD_IMPULSE}],
        summary="s", reasoning="r", complete=True,
        expectation={"structure_index": 0, "next_label": "C"})
    # Whether this particular count supports a C projection or not, the
    # field exists and is either a computed projection or an honest None.
    assert "projection" in box.submitted


def test_the_playbook_tells_the_model_to_cover_the_whole_chart_and_project():
    """Both of these were real gaps in a live run: the recent two thirds
    came back bare, and nothing said where price goes next."""
    from t3_engine.ai_advisor.playbook import ELLIOTT_PLAYBOOK

    assert "COVER THE WHOLE CHART" in ELLIOTT_PLAYBOOK
    assert "SAY WHAT COMES NEXT" in ELLIOTT_PLAYBOOK
    assert "complete=true" in ELLIOTT_PLAYBOOK
    assert "Do not supply prices" in ELLIOTT_PLAYBOOK


# ---- richer Fibonacci, and forecasting from the chart's own habits ----

def test_fibonacci_projects_from_a_third_anchor_not_the_legs_own_start():
    """Every Elliott target is a length projected from where the NEXT wave
    starts. Measuring an extension from the leg's own start is the wrong
    anchor, and that difference is the whole usefulness of the tool."""
    box = toolbox()
    plain = box.fibonacci_levels(1.0, 0, 1)
    projected = box.fibonacci_levels(1.0, 0, 1, project_from_pivot_index=2)

    assert "projections" not in plain
    assert projected["projected_from"]["pivot_index"] == 2
    assert projected["projections"] != projected["extensions"]


def test_custom_ratios_are_honoured():
    box = toolbox()
    result = box.fibonacci_levels(1.0, 0, 1, ratios=[0.5, 0.707])
    assert sorted(result["retracements"]) == ["0.500", "0.707"]


def test_confluence_only_reports_agreement_between_DIFFERENT_legs():
    """One leg agreeing with itself is not confluence, and reporting it as
    such would turn every chart into a wall of zones."""
    box = toolbox()
    result = box.fibonacci_confluence(1.0, legs=[
        {"start_pivot_index": 0, "end_pivot_index": 1},
        {"start_pivot_index": 2, "end_pivot_index": 3},
    ], tolerance_pct=0.5)

    for zone in result["zones"]:
        assert zone["legs_agreeing"] >= 2
        assert len({member.split()[0] for member in zone["from"]}) >= 2


def test_confluence_refuses_a_single_leg():
    with pytest.raises(ToolError, match="between 2 and 6 legs"):
        toolbox().fibonacci_confluence(1.0, legs=[{"start_pivot_index": 0, "end_pivot_index": 1}])


def test_swing_statistics_measure_this_chart_rather_than_a_remembered_average():
    """A guideline is only worth using if it holds on the instrument in
    front of you."""
    result = toolbox().swing_statistics(1.0)
    assert result["swings"] > 0
    assert result["median_retracement_of_previous_leg"] is not None
    assert result["up_legs"]["count"] + result["down_legs"]["count"] == result["swings"]
    assert "0.382/0.5/0.618" in result["note"]


def test_swing_statistics_refuses_a_history_too_short_to_have_habits():
    from t3_engine.ai_advisor.analyst_tools import AnalystToolbox

    box = AnalystToolbox(CANDLES, DEGREE)
    with pytest.raises(ToolError, match="too few to measure habits"):
        box.swing_statistics(20.0)      # a deviation that finds almost nothing


# ---- the 50-candle forward path ----

def test_the_projection_is_drawn_past_the_last_candle():
    """A target with no time axis is a horizontal line that never expires,
    and a path that stops at the last candle is invisible."""
    from t3_engine.ai_advisor.analyst_tools import PROJECTION_BARS, project_next_wave

    waves = [
        {"label": "1", "start_price": 100.0, "end_price": 120.0, "end_time": 0},
        {"label": "2", "start_price": 120.0, "end_price": 110.0, "end_time": 300},
        {"label": "3", "start_price": 110.0, "end_price": 150.0, "end_time": 600},
        {"label": "4", "start_price": 150.0, "end_price": 138.0, "end_time": 900},
    ]
    projection = project_next_wave(waves, "5", bar_seconds=300)

    assert projection["bars_ahead"] == PROJECTION_BARS
    assert len(projection["path"]) == PROJECTION_BARS + 1
    assert projection["path"][0] == {"time": 900, "price": 138.0}
    assert projection["path"][-1]["time"] == 900 + 300 * PROJECTION_BARS
    times = [point["time"] for point in projection["path"]]
    assert times == sorted(times)            # the chart needs them ordered


def test_the_path_runs_to_the_middle_ratio_not_an_extreme():
    """A path drawn to a tail of the distribution gets read as a forecast
    of the tail."""
    from t3_engine.ai_advisor.analyst_tools import project_next_wave

    waves = [
        {"label": "1", "start_price": 100.0, "end_price": 120.0, "end_time": 0},
        {"label": "2", "start_price": 120.0, "end_price": 110.0, "end_time": 300},
        {"label": "3", "start_price": 110.0, "end_price": 150.0, "end_time": 600},
        {"label": "4", "start_price": 150.0, "end_price": 138.0, "end_time": 900},
    ]
    projection = project_next_wave(waves, "5", bar_seconds=300)
    primary = [t for t in projection["targets"] if t["primary"]]

    assert len(primary) == 1
    assert primary[0]["price"] == projection["primary_target"]
    assert projection["path"][-1]["price"] == projection["primary_target"]
    prices = [t["price"] for t in projection["targets"]]
    assert min(prices) < projection["primary_target"] < max(prices)


def test_an_expected_duration_shortens_the_reach_but_not_the_horizon():
    """The model says how long it expects the wave to take; the chart still
    shows the same window, so two runs stay visually comparable."""
    from t3_engine.ai_advisor.analyst_tools import project_next_wave

    waves = [
        {"label": "A", "start_price": 150.0, "end_price": 130.0, "end_time": 0},
        {"label": "B", "start_price": 130.0, "end_price": 142.0, "end_time": 300},
    ]
    quick = project_next_wave(waves, "C", bar_seconds=300, expected_bars=10)
    assert quick["expected_bars"] == 10
    assert quick["bars_ahead"] == 50
    # Reached the target by bar 10, then held flat to the horizon.
    assert quick["path"][10]["price"] == quick["primary_target"]
    assert quick["path"][-1]["price"] == quick["primary_target"]


def test_the_playbook_teaches_confluence_projection_anchors_and_alternation():
    from t3_engine.ai_advisor.playbook import ELLIOTT_PLAYBOOK

    assert "project_from_pivot_index" in ELLIOTT_PLAYBOOK
    assert "fibonacci_confluence" in ELLIOTT_PLAYBOOK
    assert "ALTERNATION IS A FORECAST" in ELLIOTT_PLAYBOOK
    assert "swing_statistics" in ELLIOTT_PLAYBOOK


# ---- volume and momentum, the two things the model kept reaching for ----

def test_volume_profile_measures_one_wave_at_a_time():
    """Wave 3 normally carries the heaviest volume of an impulse and wave 5
    makes its higher high on less. That comparison is only possible if each
    wave can be measured on its own."""
    toolbox = AnalystToolbox(CANDLES, Timeframe.M5, "TEST")
    result = toolbox.volume_profile(0, 40)
    assert result["bars"] == 41
    assert result["total_volume"] > 0
    assert result["average_volume_per_bar"] > 0
    # taker_buy_share is a fraction of the volume that hit the ask
    assert 0.0 <= result["taker_buy_share"] <= 1.0
    assert 0 <= result["busiest_bar"]["index"] < len(CANDLES)
    # ...and the two halves of a chart really do differ
    other = toolbox.volume_profile(41, 90)
    assert other["total_volume"] != result["total_volume"]


def test_momentum_never_sees_past_the_bar_it_is_asked_about():
    """The whole no-lookahead guarantee in one tool: indicators are fed the
    history up to end_index and not one candle further, so what comes back
    is what was knowable there."""
    toolbox = AnalystToolbox(CANDLES, Timeframe.M5, "TEST")
    early = toolbox.momentum(0, 40)
    late = toolbox.momentum(0, len(CANDLES) - 1)
    assert early["end_index"] == 40
    assert early["macd"] != late["macd"], "a later bar must give a different reading"
    assert early["adx"] is not None and early["ema9"] is not None


def test_momentum_reports_the_macd_extreme_inside_the_range():
    """Divergence is read by comparing one wave's MACD peak against
    another's - the value at the final bar alone cannot show it."""
    toolbox = AnalystToolbox(CANDLES, Timeframe.M5, "TEST")
    result = toolbox.momentum(20, 70)
    peak = result["macd_peak_in_range"]
    trough = result["macd_trough_in_range"]
    assert peak and trough
    assert 20 <= peak["index"] <= 70 and 20 <= trough["index"] <= 70
    assert peak["value"] >= trough["value"]


def test_both_new_tools_refuse_a_bad_range_with_a_readable_reason():
    """The model reads tool errors and retries, which is only possible if
    the error says what was wrong."""
    toolbox = AnalystToolbox(CANDLES, Timeframe.M5, "TEST")
    for call in (toolbox.volume_profile, toolbox.momentum):
        with pytest.raises(ToolError) as exc:
            call(50, 10)
        assert "must be before" in str(exc.value)
        with pytest.raises(ToolError):
            call(0, len(CANDLES) + 500)


def test_the_playbook_tells_the_agent_when_to_use_them():
    """A tool the model does not know when to reach for is a tool it does
    not have."""
    from t3_engine.ai_advisor.playbook import ELLIOTT_PLAYBOOK
    assert "volume_profile" in ELLIOTT_PLAYBOOK
    assert "momentum" in ELLIOTT_PLAYBOOK
    assert "macd_peak_in_range" in ELLIOTT_PLAYBOOK
    # and that they inform a count rather than overrule the hard rules.
    # Whitespace is normalised first: the playbook is hard-wrapped prose,
    # so a phrase that happens to straddle a line break is still present.
    flowing = " ".join(ELLIOTT_PLAYBOOK.split())
    assert "never override the hard rules" in flowing


# ---- a run must not lose everything to one upstream hiccup -------------
# Measured failure: a 15m run made nine paid calls, the tenth came back
# "Provider returned an empty response", and the chart stayed blank. The
# work was real, the money was spent, and nothing survived.

def test_the_model_is_asked_to_bank_a_partial_count_halfway_through():
    """submit_count is additive, so banking early costs nothing and is the
    difference between a partial count and no count at all."""
    from t3_engine.ai_advisor.analyst import BANK_PARTIAL_NUDGE
    client, _ = scripted_client([function_call_turn("list_pivots", {"deviation_pct": 1.0})])
    result = run_analyst("sk-test", CANDLES, DEGREE, client=client, max_steps=8)

    nudges = [e for e in result.transcript
              if e["role"] == "system" and e["text"] == BANK_PARTIAL_NUDGE]
    assert len(nudges) == 1, "asked exactly once, not every step"
    assert nudges[0]["step"] == 5          # halfway (8 // 2) + 1
    assert "complete=false" in BANK_PARTIAL_NUDGE
    assert "keep working" in BANK_PARTIAL_NUDGE


def test_a_run_that_has_already_submitted_is_not_nudged_to_bank():
    """The nudge exists to protect unsaved work. There is none to protect
    once the model has submitted."""
    from t3_engine.ai_advisor.analyst import BANK_PARTIAL_NUDGE
    client, _ = scripted_client([function_call_turn("submit_count", {
        "structures": [{"structure": "IMPULSE", "deviation_pct": 1.0,
                        "direction": "UP", "waves": GOOD_IMPULSE}],
        "summary": "s", "reasoning": "r", "complete": False})])
    result = run_analyst("sk-test", CANDLES, DEGREE, client=client, max_steps=8)

    assert not [e for e in result.transcript
                if e["role"] == "system" and e["text"] == BANK_PARTIAL_NUDGE]


def test_the_two_nudges_never_land_in_the_same_turn():
    """"Bank this and carry on" next to "this is your LAST step" is two
    contradictory instructions in one turn."""
    from t3_engine.ai_advisor.analyst import BANK_PARTIAL_NUDGE, FINAL_STEP_NUDGE
    for budget in (1, 2, 3, 8, 24):
        client, _ = scripted_client([function_call_turn("list_pivots", {"deviation_pct": 1.0})])
        result = run_analyst("sk-test", CANDLES, DEGREE, client=client, max_steps=budget)
        by_step = {}
        for entry in result.transcript:
            if entry["role"] == "system":
                by_step.setdefault(entry["step"], []).append(entry["text"])
        for texts in by_step.values():
            assert not (BANK_PARTIAL_NUDGE in texts and FINAL_STEP_NUDGE in texts), \
                f"both nudges in one turn at max_steps={budget}"


def test_an_empty_upstream_response_is_retried_rather_than_fatal():
    """"Provider returned an empty response" is the upstream answering with
    nothing - a one-off hiccup, not a verdict on the request. It used to
    discard nine steps of paid work."""
    from t3_engine.ai_advisor.advisor import is_transient
    assert is_transient("Provider returned an empty response")
    assert is_transient("upstream returned an empty completion")
    # ...while a real refusal still is not retried
    assert not is_transient("invalid api key")
    assert not is_transient("this model does not support tools")


def test_the_output_budget_leaves_room_for_thinking_and_the_answer():
    """Reasoning tokens count against max_tokens, so at `max` effort the
    model can spend the entire budget thinking and emit nothing - which the
    provider reports as an empty response."""
    from t3_engine.ai_advisor.analyst import ANALYST_MAX_OUTPUT_TOKENS
    assert ANALYST_MAX_OUTPUT_TOKENS >= 32768
