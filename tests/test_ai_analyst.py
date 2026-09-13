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
            "summary": "s", "reasoning": "r",
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
            "summary": "s", "reasoning": "r",
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
    assert "all 4 steps" in result.note


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
            "summary": "s", "reasoning": "r",
        }),
    ])
    result = run_analyst("sk-test", CANDLES, DEGREE, client=client)

    assert result.finished
    assert "error" in result.steps[0].result_summary
    handed_back = json.loads(sent[1]["messages"][3]["content"])
    assert "does not exist" in handed_back["error"]


def test_no_api_key_fails_before_anything_is_sent():
    with pytest.raises(AIAdvisorError, match="No NVIDIA API Catalog API key"):
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
