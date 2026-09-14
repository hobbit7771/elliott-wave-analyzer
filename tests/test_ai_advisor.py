"""OpenRouter client tests.

Every one runs against a mocked transport - this sandbox has no outbound
access to openrouter.ai, and more importantly the whole point of
the design is that nothing downstream depends on a live model call
succeeding.

Chat calls STREAM, so most responses here are SSE frames. The streaming
path is where the subtle bugs live (tool-call arguments arrive a few
characters at a time, keyed by index), and it is also the mitigation for
the read timeout that killed a production run - a silent socket during a
reasoning model's thinking phase."""

import json

import httpx
import pytest

from t3_engine.ai_advisor.advisor import (
    AIAdvisorError,
    request_commentary,
    request_wave_count,
)


def make_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def sse(*chunks, done: bool = True) -> str:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return body + ("data: [DONE]\n\n" if done else "")


def chat_response(text: str, finish_reason: str = "stop") -> httpx.Response:
    """A streamed answer delivered in several content deltas - which is how
    a real one arrives, and what the reassembler has to put back together."""
    midpoint = len(text) // 2
    frames = [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": text[:midpoint]}}]},
        {"choices": [{"index": 0, "delta": {"content": text[midpoint:]},
                      "finish_reason": finish_reason}]},
    ]
    return httpx.Response(200, text=sse(*frames))


def plain_response(text: str, finish_reason: str = "stop") -> httpx.Response:
    """Non-streaming shape, used by ping."""
    return httpx.Response(200, json={
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": finish_reason}],
    })


def test_request_commentary_parses_a_chat_completion():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-test"
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        return chat_response("This wave 3 count looks reasonable but watch for extension.")

    result = request_commentary("sk-test", {"wave": "3", "confidence": 82}, client=make_client(handler))
    assert "wave 3" in result.text.lower()


def test_commentary_sends_the_instruction_as_a_system_message_not_a_user_turn():
    """Pivot data and engine state are DATA. Keeping the instruction in its
    own system message is what stops a number in the payload from reading
    as a new instruction to the model."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return chat_response("ok")

    request_commentary("sk-test", {"wave": "3"}, client=make_client(handler))
    assert seen["messages"][0]["role"] == "system"
    assert seen["messages"][1]["role"] == "user"
    assert "3" in seen["messages"][1]["content"]


def test_request_commentary_raises_without_key():
    with pytest.raises(AIAdvisorError, match="No OpenRouter API key"):
        request_commentary("", {"wave": "3"})


def test_request_commentary_raises_on_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="invalid request")

    with pytest.raises(AIAdvisorError, match="OpenRouter API error 400"):
        request_commentary("sk-bad", {"wave": "3"}, client=make_client(handler))


def test_a_missing_model_404_is_passed_through_verbatim_with_a_usable_hint():
    """A router's catalogue churns constantly - free tiers get renamed and
    retired far more often than keys get revoked. The user needs BOTH
    halves: the API's message and where to act on it, so neither may be
    swallowed into a generic "AI unavailable"."""
    router_message = '{"error": {"code": 404, "message": "Model made-up/model not found."}}'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text=router_message)

    with pytest.raises(AIAdvisorError) as excinfo:
        request_commentary("sk-test", {"wave": "3"}, model="made-up/model",
                           client=make_client(handler))
    message = str(excinfo.value)
    assert "not found" in message                 # the API's own words survive
    assert "made-up/model" in message
    assert "Model field" in message               # and where to act on it
    assert "API base URL" in message              # 404 has two causes here, both fixable in the UI


def test_auth_and_parameter_errors_point_at_the_right_cause():
    """Failures that all look like "the AI is broken" from the outside, and
    have completely different fixes."""
    cases = [
        (403, "permission denied", "Check the API key itself"),
        (400, "unknown parameter", "rejected parameter"),
    ]
    for status, body, expected in cases:
        def handler(request: httpx.Request, _status=status, _body=body) -> httpx.Response:
            return httpx.Response(_status, text=_body)

        with pytest.raises(AIAdvisorError, match=expected):
            request_commentary("sk-test", {"wave": "3"}, client=make_client(handler))


def test_the_api_base_url_is_overridable_without_a_redeploy():
    """The endpoint could not be verified from the build sandbox (the
    egress proxy blocks orcarouter.ai), so the base URL is a field like the
    model id is: if the real path differs, that is a paste rather than a
    redeploy."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return chat_response("ok")

    request_commentary("sk-test", {"wave": "3"}, base_url="https://openrouter.ai/v2",
                       client=make_client(handler))
    assert seen["url"] == "https://openrouter.ai/v2/chat/completions"


def test_a_pasted_full_endpoint_is_not_doubled_up():
    """People paste the whole endpoint they see in a docs page far more
    often than they paste a bare base, and '/chat/completions/chat/
    completions' is a confusing 404 to debug."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return chat_response("ok")

    request_commentary("sk-test", {"wave": "3"},
                       base_url="https://openrouter.ai/api/v1/chat/completions/",
                       client=make_client(handler))
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"


def test_the_model_id_actually_reaches_the_request_body():
    """The Model field is only useful if it's what gets called. On a router
    the model is a body field, not part of the URL."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return chat_response("ok")

    request_commentary("sk-test", {"wave": "3"}, model="nvidia/nemotron-3-ultra-550b-a55b:free",
                       client=make_client(handler))
    assert seen["model"] == "nvidia/nemotron-3-ultra-550b-a55b:free"


def test_an_error_body_returned_with_http_200_is_still_an_error():
    """A router can answer 200 while the upstream vendor refused. Treating
    that as a normal empty answer is how "the AI had no concerns" gets
    printed when the call in fact failed."""
    def handler(request: httpx.Request) -> httpx.Response:
        # Mid-stream error frame: the headers already said 200.
        return httpx.Response(200, text=sse({"error": {"message": "upstream provider returned no completion"}}))

    with pytest.raises(AIAdvisorError, match="upstream provider"):
        request_commentary("sk-test", {"wave": "3"}, client=make_client(handler))


def test_a_truncated_answer_is_reported_as_a_token_budget_problem():
    """Reasoning models spend output tokens before the visible answer, so a
    cut-off answer must not surface as a formatting error - that sends you
    debugging the wrong thing."""
    def handler(request: httpx.Request) -> httpx.Response:
        return chat_response("The engine rightly rejected this trade due to drawdown limits, but the",
                             finish_reason="length")

    with pytest.raises(AIAdvisorError, match="ran out of output tokens"):
        request_commentary("sk-test", {"wave": "3"}, client=make_client(handler))


def test_empty_answer_text_is_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return chat_response("   ")

    with pytest.raises(AIAdvisorError, match="empty answer"):
        request_commentary("sk-test", {"wave": "3"}, client=make_client(handler))


def test_a_connect_failure_points_at_the_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    with pytest.raises(AIAdvisorError, match="Could not connect"):
        request_commentary("sk-test", {"wave": "3"}, client=make_client(handler))


def test_a_read_timeout_is_not_reported_as_an_unreachable_endpoint():
    """This is the production failure it fixes: a read timeout was printed
    as "could not access the API", which is the wrong diagnosis. The
    connection WORKED - the model was still thinking - and that has a
    completely different fix from a wrong URL."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(AIAdvisorError) as excinfo:
        request_commentary("sk-test", {"wave": "3"}, timeout=180, client=make_client(handler))
    message = str(excinfo.value)
    assert "did not answer within 180s" in message
    assert "connection to" in message and "worked" in message
    assert "Could not connect" not in message
    assert "Raise the Timeout field" in message


def test_connecting_is_not_given_the_long_read_budget():
    """One scalar timeout would make an unreachable host hang for the full
    read budget before admitting it. Connect fails fast; reads are patient."""
    from t3_engine.ai_advisor.advisor import CONNECT_TIMEOUT, build_timeout

    timeout = build_timeout(300)
    assert timeout.connect == CONNECT_TIMEOUT
    assert timeout.read == 300


def test_the_read_timeout_is_bounded_at_both_ends():
    from t3_engine.ai_advisor.advisor import MAX_READ_TIMEOUT, build_timeout

    assert build_timeout(1).read == 10.0
    assert build_timeout(99999).read == MAX_READ_TIMEOUT


def test_ping_confirms_the_key_url_and_model_from_the_first_token():
    """A wrong key, a wrong URL, a dead model id and a model that merely
    thinks for four minutes all look identical from the dashboard. This
    separates them - and it STREAMS, because a non-streaming check against
    a reasoning model waits out the whole thinking phase, making the one
    call meant to diagnose slowness the likeliest to time out."""
    from t3_engine.ai_advisor.advisor import ping

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return chat_response("ok")

    result = ping("sk-test", model="nvidia/nemotron-3-ultra-550b-a55b:free", client=make_client(handler))
    assert result["ok"] is True
    assert result["answer"].startswith("o")
    assert result["endpoint"].endswith("/chat/completions")
    assert seen["stream"] is True
    # No max_tokens cap: on most APIs a reasoning model's thinking counts
    # against it, so a small cap can end the generation before any visible
    # token exists - indistinguishable from a hang.
    assert "max_tokens" not in seen


def test_ping_returns_on_the_first_token_without_waiting_for_the_rest():
    """The question is "does this setup produce tokens". Waiting for the
    whole answer only risks the timeout the check exists to diagnose."""
    from t3_engine.ai_advisor.advisor import ping

    frames = sse(
        {"choices": [{"index": 0, "delta": {"content": "ok"}}]},
        {"choices": [{"index": 0, "delta": {"content": " and here is a very long tail"}}]},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=frames)

    result = ping("sk-test", client=make_client(handler))
    assert result["answer"] == "ok"        # stopped at the first token, not the last


def test_ping_counts_thinking_as_a_sign_of_life():
    """A reasoning model emits its thinking first. That is still proof the
    key, URL and model all work - refusing to count it would report a
    working setup as broken."""
    from t3_engine.ai_advisor.advisor import ping

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse(
            {"choices": [{"index": 0, "delta": {"reasoning_content": "Let me think..."}}]}))

    result = ping("sk-test", client=make_client(handler))
    assert result["ok"] is True
    assert result["reasoning_first"] is True


def test_ping_reports_the_number_that_decides_whether_an_agent_run_is_feasible():
    """The analyst pays the time-to-first-token once per step, so "it
    works" is not the useful answer - "it works, 40s per step" is."""
    from t3_engine.ai_advisor.advisor import _speed_advice, ping

    def handler(request: httpx.Request) -> httpx.Response:
        return chat_response("ok")

    result = ping("sk-test", client=make_client(handler))
    assert "seconds_to_first_token" in result
    assert "advice" in result

    assert "any step budget" in _speed_advice(1.0)
    assert "keep the step budget modest" in _speed_advice(12.0)
    assert "cut the step budget" in _speed_advice(45.0)


def test_a_stream_that_closes_without_a_single_token_says_the_setup_is_fine():
    """Distinguishing "your key is wrong" from "this model produced
    nothing" is the whole job of this check."""
    from t3_engine.ai_advisor.advisor import ping

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="data: [DONE]\n\n")

    with pytest.raises(AIAdvisorError) as excinfo:
        ping("sk-test", client=make_client(handler))
    assert "are therefore fine" in str(excinfo.value)
    assert "Try another model id" in str(excinfo.value)


# ---- wave-count proposals ----

SAMPLE_PIVOTS = [
    {"index": 0, "time": 0, "price": 100.0, "kind": "LOW"},
    {"index": 1, "time": 60, "price": 150.0, "kind": "HIGH"},
]


def test_request_wave_count_parses_structured_json():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        # A count is an analysis, not a creative task: mildly warm so the
        # agent explores, never hot enough to invent.
        assert body["temperature"] <= 0.4
        assert body["response_format"] == {"type": "json_object"}
        return chat_response(json.dumps({
            "waves": [{"label": "1", "start_pivot_index": 0, "end_pivot_index": 1}],
            "reasoning": "Clean impulse off the low.",
        }))

    proposal = request_wave_count("sk-test", SAMPLE_PIVOTS, "UP", client=make_client(handler))
    assert proposal.waves == [{"label": "1", "start_pivot_index": 0, "end_pivot_index": 1}]
    assert "impulse" in proposal.reasoning


def test_request_wave_count_tolerates_markdown_fenced_json():
    """Models wrap JSON in ``` fences even when told not to - that's a
    formatting quirk, not a reason to throw the answer away."""
    fenced = '```json\n{"waves": [{"label": "1", "start_pivot_index": 0, "end_pivot_index": 1}]}\n```'

    def handler(request: httpx.Request) -> httpx.Response:
        return chat_response(fenced)

    proposal = request_wave_count("sk-test", SAMPLE_PIVOTS, "UP", client=make_client(handler))
    assert proposal.waves[0]["label"] == "1"


def test_request_wave_count_rejects_non_json_prose():
    def handler(request: httpx.Request) -> httpx.Response:
        return chat_response("I think this is a wave 3, roughly speaking.")

    with pytest.raises(AIAdvisorError, match="did not return valid JSON"):
        request_wave_count("sk-test", SAMPLE_PIVOTS, "UP", client=make_client(handler))


def test_request_wave_count_rejects_json_without_a_waves_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return chat_response(json.dumps({"analysis": "looks bullish"}))

    with pytest.raises(AIAdvisorError, match="no 'waves' key"):
        request_wave_count("sk-test", SAMPLE_PIVOTS, "UP", client=make_client(handler))


# ---- streaming ----
# Chat calls stream. This is where the subtle bugs are: tool-call arguments
# arrive a few characters at a time, keyed by index, and a naive
# concatenation produces JSON that parses fine while describing a wave
# nobody proposed.

def test_chat_calls_stream_by_default():
    """The point is not elegance. A reasoning model can think for minutes
    before its first visible token, and a non-streaming request spends that
    whole time on a silent socket - which production hit as a read timeout
    that threw away the entire run."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        seen["accept"] = request.headers.get("accept")
        return chat_response("ok")

    request_commentary("sk-test", {"wave": "3"}, client=make_client(handler))
    assert seen["stream"] is True
    assert seen["accept"] == "text/event-stream"


def test_content_deltas_are_reassembled_in_order():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse(
            {"choices": [{"index": 0, "delta": {"content": "Wave "}}]},
            {"choices": [{"index": 0, "delta": {"content": "three "}}]},
            {"choices": [{"index": 0, "delta": {"content": "is extended."},
                          "finish_reason": "stop"}]},
        ))

    result = request_commentary("sk-test", {"wave": "3"}, client=make_client(handler))
    assert result.text == "Wave three is extended."


def test_tool_call_arguments_are_stitched_back_together():
    """Arguments arrive as a character stream. Getting this wrong yields a
    tool call whose JSON parses but whose contents were never proposed."""
    from t3_engine.ai_advisor.advisor import _consume_stream

    frames = [
        'data: ' + json.dumps({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_a", "type": "function",
             "function": {"name": "list_pi", "arguments": '{"devi'}}]}}]}),
        'data: ' + json.dumps({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"name": "vots", "arguments": 'ation_pct": '}}]}}]}),
        'data: ' + json.dumps({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '3.0}'}}]}, "finish_reason": "tool_calls"}]}),
        'data: [DONE]',
    ]
    assembled = _consume_stream(frames, "m")
    call = assembled["choices"][0]["message"]["tool_calls"][0]
    assert call["id"] == "call_a"
    assert call["function"]["name"] == "list_pivots"
    assert json.loads(call["function"]["arguments"]) == {"deviation_pct": 3.0}
    assert assembled["choices"][0]["finish_reason"] == "tool_calls"


def test_parallel_tool_calls_are_kept_apart_by_index():
    """Two calls streaming at once interleave. Keying on arrival order
    instead of `index` splices one call's arguments into the other's."""
    from t3_engine.ai_advisor.advisor import _consume_stream

    frames = [
        'data: ' + json.dumps({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "a", "function": {"name": "measure_move", "arguments": '{"x":'}},
            {"index": 1, "id": "b", "function": {"name": "fibonacci_levels", "arguments": '{"y":'}}]}}]}),
        'data: ' + json.dumps({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 1, "function": {"arguments": '2}'}},
            {"index": 0, "function": {"arguments": '1}'}}]}}]}),
        'data: [DONE]',
    ]
    calls = _consume_stream(frames, "m")["choices"][0]["message"]["tool_calls"]
    assert [c["id"] for c in calls] == ["a", "b"]
    assert json.loads(calls[0]["function"]["arguments"]) == {"x": 1}
    assert json.loads(calls[1]["function"]["arguments"]) == {"y": 2}


def test_keepalive_and_unparsable_frames_are_skipped_not_fatal():
    from t3_engine.ai_advisor.advisor import _consume_stream

    frames = [
        ": keep-alive",
        "",
        "data: not json at all",
        'data: ' + json.dumps({"choices": [{"index": 0, "delta": {"content": "fine"},
                                            "finish_reason": "stop"}]}),
        "data: [DONE]",
    ]
    assert _consume_stream(frames, "m")["choices"][0]["message"]["content"] == "fine"


def test_a_stream_that_carries_no_completion_chunks_is_an_error():
    """An empty stream must not read as an empty answer - in a trading tool
    that means "the model had no concerns"."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="data: [DONE]\n\n")

    with pytest.raises(AIAdvisorError, match="no completion chunks"):
        request_commentary("sk-test", {"wave": "3"}, client=make_client(handler))


def test_an_http_error_in_stream_mode_still_reads_its_body():
    """In stream mode the body is not loaded yet when the status arrives;
    without an explicit read the error message would be empty."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid api key")

    with pytest.raises(AIAdvisorError, match="invalid api key"):
        request_commentary("sk-test", {"wave": "3"}, client=make_client(handler))


# ---- optional model options ----

def test_seed_is_not_sent_by_default():
    """At temperature 0 decoding is already deterministic, so a seed adds
    nothing while remaining one more parameter a provider can reject with a
    400 - on an endpoint the user is free to point anywhere."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return chat_response("ok")

    request_commentary("sk-test", {"wave": "3"}, client=make_client(handler))
    assert "seed" not in seen

    seen.clear()
    request_commentary("sk-test", {"wave": "3"}, seed=42, client=make_client(handler))
    assert seen["seed"] == 42


def test_analysis_and_commentary_both_run_only_mildly_warm():
    """Set to 0.3 on the project owner's instruction, and the reasoning is
    worth keeping visible: the agent loop EXPLORES over a dozen-plus steps,
    and a little sampling lets it try a different degree or anchor instead
    of walking the same path to the same local answer. Every structure is
    re-validated server-side, so a worse count costs a step, not a bad
    label. What it must never be is HOT - a labelling run at 0.9 is
    inventing, not reading."""
    from t3_engine.ai_advisor.advisor import request_wave_count

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return chat_response(json.dumps({"waves": []}))

    request_wave_count("sk-test", SAMPLE_PIVOTS, "UP", client=make_client(handler))
    assert seen["temperature"] == 0.3

    seen.clear()

    def prose(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return chat_response("a second opinion")

    request_commentary("sk-test", {"wave": "3"}, client=make_client(prose))
    assert 0 < seen["temperature"] <= 0.4


def test_reasoning_effort_is_absent_unless_asked_for():
    """Effort rides inside OpenRouter's own `reasoning` object, and an unset
    field is an ABSENT field - an unsupported parameter is a 400, not a
    graceful ignore, on an endpoint the user can point anywhere."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return chat_response("ok")

    request_commentary("sk-test", {"wave": "3"}, client=make_client(handler))
    assert "effort" not in seen.get("reasoning", {})

    seen.clear()
    request_commentary("sk-test", {"wave": "3"}, reasoning_effort="high", client=make_client(handler))
    assert seen["reasoning"]["effort"] == "high"


def test_an_invalid_reasoning_effort_is_refused_before_the_request():
    with pytest.raises(AIAdvisorError, match="reasoning_effort must be one of"):
        request_commentary("sk-test", {"wave": "3"}, reasoning_effort="maximum")


def test_seed_can_be_turned_off_entirely():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return chat_response("ok")

    request_commentary("sk-test", {"wave": "3"}, seed=None, client=make_client(handler))
    assert "seed" not in seen


# ---- rate limiting ----
# The dominant failure on a free tier, and it is a WAIT rather than a
# defect: the run was going fine and the quota window closed. Retrying
# turns "the analyst died at step 5" into "the analyst paused at step 5".

def test_a_rate_limit_is_retried_before_it_is_reported_as_a_failure(monkeypatch):
    slept = []
    monkeypatch.setattr("t3_engine.ai_advisor.advisor._sleep", slept.append)
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(429, text='{"status":429,"title":"Too Many Requests"}')
        return chat_response("recovered")

    result = request_commentary("sk-test", {"wave": "3"}, client=make_client(handler))
    assert result.text == "recovered"
    assert len(attempts) == 3
    assert slept == [4.0, 12.0]          # backoff grows between attempts


def test_the_servers_own_retry_after_wins_over_the_local_backoff(monkeypatch):
    """Guessing shorter than the quota window burns another attempt and on
    some services extends the ban."""
    slept = []
    monkeypatch.setattr("t3_engine.ai_advisor.advisor._sleep", slept.append)
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, text="slow down", headers={"Retry-After": "7"})
        return chat_response("ok")

    request_commentary("sk-test", {"wave": "3"}, client=make_client(handler))
    assert slept == [7.0]


def test_an_absurd_retry_after_is_capped(monkeypatch):
    slept = []
    monkeypatch.setattr("t3_engine.ai_advisor.advisor._sleep", slept.append)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="nope", headers={"Retry-After": "3600"})

    with pytest.raises(AIAdvisorError):
        request_commentary("sk-test", {"wave": "3"}, client=make_client(handler))
    assert all(s <= 60.0 for s in slept)


def test_a_persistent_rate_limit_finally_fails_with_advice_that_fits(monkeypatch):
    """After the retries, the message has to say what the user can change -
    'rate limited' alone leaves them clicking the same button again."""
    monkeypatch.setattr("t3_engine.ai_advisor.advisor._sleep", lambda s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text='{"status":429,"title":"Too Many Requests"}')

    with pytest.raises(AIAdvisorError) as excinfo:
        request_commentary("sk-test", {"wave": "3"}, client=make_client(handler))
    message = str(excinfo.value)
    assert "automatic retries" in message
    assert "lower the step budget" in message
    assert "Too Many Requests" in message      # the API's own words survive


def test_retries_apply_to_the_streaming_path_too(monkeypatch):
    """Chat calls stream, so a retry policy that only covered the plain
    path would never fire where it is actually needed."""
    monkeypatch.setattr("t3_engine.ai_advisor.advisor._sleep", lambda s: None)
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(json.loads(request.content)["stream"])
        if len(attempts) == 1:
            return httpx.Response(429, text="wait")
        return chat_response("streamed fine")

    result = request_commentary("sk-test", {"wave": "3"}, client=make_client(handler))
    assert result.text == "streamed fine"
    assert attempts == [True, True]


# ---- diagnosis ----
# Every other error path turns a failure into a sentence written from an
# assumption about the cause. These return observations instead.

def test_access_check_verifies_key_and_url_without_running_a_model():
    """The check that splits the problem in two. A catalogue listing needs
    the key and hits the same base URL, but runs no inference - so if it
    answers fast, nothing on this side is broken and the wait belongs to
    the model."""
    from t3_engine.ai_advisor.advisor import check_access

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        return httpx.Response(200, json={"data": [{"id": "nvidia/nemotron-3-ultra-550b-a55b:free",
                                                   "supported_parameters": ["tools", "temperature"]},
                                                  {"id": "some/embedding-model"}]})

    result = check_access("sk-test", client=make_client(handler))
    assert seen["method"] == "GET"
    assert seen["url"] == "https://openrouter.ai/api/v1/models"
    assert result["model_count"] == 2
    assert "nvidia/nemotron-3-ultra-550b-a55b:free" in result["models"]


def test_a_failing_access_check_says_it_is_not_about_model_speed():
    from t3_engine.ai_advisor.advisor import check_access

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid key")

    with pytest.raises(AIAdvisorError) as excinfo:
        check_access("sk-test", client=make_client(handler))
    assert "runs no model at all" in str(excinfo.value)


def test_the_access_check_tolerates_a_base_url_with_the_endpoint_pasted_on():
    from t3_engine.ai_advisor.advisor import check_access

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": []})

    check_access("sk-test", base_url="https://openrouter.ai/api/v1/chat/completions",
                 client=make_client(handler))
    assert seen["url"] == "https://openrouter.ai/api/v1/models"


def test_diagnose_reports_observations_and_never_raises_on_a_timeout():
    """A timeout IS the observation, and the partial result is the
    evidence. Raising would throw away the only data the run produced."""
    from t3_engine.ai_advisor.advisor import diagnose

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    report = diagnose("sk-test", client=make_client(handler))
    assert "error" in report
    assert "did not answer" in report["error"]
    assert "never got a response header" in report["verdict"]


def test_diagnose_recognises_a_buffered_non_sse_answer():
    """Some gateways ignore stream: true. The SSE reader would wait out the
    whole generation and then blame the model for an empty stream."""
    from t3_engine.ai_advisor.advisor import diagnose

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]},
                              headers={"content-type": "application/json"})

    report = diagnose("sk-test", client=make_client(handler))
    assert report["status"] == 200
    assert report["looks_like_sse"] is False
    assert "NOT server-sent events" in report["verdict"]


def test_a_buffered_answer_is_still_read_rather_than_thrown_away():
    """Recognising the gateway's behaviour is not enough - the answer it
    did send has to survive."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "buffered answer"},
                         "finish_reason": "stop"}]},
            headers={"content-type": "application/json"})

    result = request_commentary("sk-test", {"wave": "3"}, client=make_client(handler))
    assert result.text == "buffered answer"


def test_diagnose_calls_out_a_202_queue_response():
    """202 is "come back later", not an answer. A plain chat client waits
    forever on it, which is indistinguishable from a slow model."""
    from t3_engine.ai_advisor.advisor import diagnose

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"reqId": "abc"}, headers={"nvcf-reqid": "abc"})

    report = diagnose("sk-test", client=make_client(handler))
    assert report["status"] == 202
    assert "queued the request" in report["verdict"]
    assert report["headers"].get("nvcf-reqid") == "abc"


def test_diagnose_reports_a_healthy_stream_with_its_timings():
    from t3_engine.ai_advisor.advisor import diagnose

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse({"choices": [{"index": 0, "delta": {"content": "ok"}}]}),
                              headers={"content-type": "text/event-stream"})

    report = diagnose("sk-test", client=make_client(handler))
    assert report["verdict"].startswith("Healthy")
    assert report["bytes_received"] > 0
    assert "first_bytes" in report


def test_diagnose_only_echoes_an_allowlist_of_response_headers():
    """A diagnostic that echoes arbitrary upstream headers is one change
    away from leaking something."""
    from t3_engine.ai_advisor.advisor import diagnose

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="data: [DONE]\n\n", headers={
            "content-type": "text/event-stream", "set-cookie": "session=secret",
            "x-internal-token": "do-not-echo"})

    report = diagnose("sk-test", client=make_client(handler))
    assert "set-cookie" not in report["headers"]
    assert "x-internal-token" not in report["headers"]
    assert "content-type" in report["headers"]


# ---- configuration probe ----
# The endpoint diagnostic proved the catalogue answers in 0.08s while the
# chat endpoint sends no headers for 45s. That settles "is it our side"
# (no) but not "which request setting makes the generation long". Each
# variant differs by exactly one thing, so the first that answers names the
# cause outright.

def _variant_handler(works):
    """`works` decides, from the parsed body, whether this variant answers."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if not works(body):
            raise httpx.ReadTimeout("timed out", request=request)
        if body.get("stream"):
            return httpx.Response(200, text=sse({"choices": [{"index": 0,
                                                              "delta": {"content": "ok"}}]}))
        return plain_response("ok")
    return handler


def test_the_probe_pins_thinking_mode_as_the_cause_when_it_is():
    from t3_engine.ai_advisor.advisor import probe_variants

    thinking_off_only = _variant_handler(
        lambda b: (b.get("reasoning") or {}).get("enabled") is False)
    report = probe_variants("sk-test", client=make_client(thinking_off_only))

    by_label = {v["variant"]: v for v in report["variants"]}
    assert by_label["thinking off, streamed"]["ok"] is True
    assert by_label["thinking on, streamed"]["ok"] is False
    assert "Thinking mode is the cause" in report["verdict"]
    assert "Leave 'Model thinking' off" in report["verdict"]


def test_the_probe_notices_a_gateway_that_will_not_stream():
    from t3_engine.ai_advisor.advisor import probe_variants

    report = probe_variants("sk-test",
                            client=make_client(_variant_handler(lambda b: not b.get("stream"))))
    assert "does not serve SSE" in report["verdict"]


def test_the_probe_says_so_when_nothing_works_at_all():
    from t3_engine.ai_advisor.advisor import probe_variants

    report = probe_variants("sk-test", client=make_client(_variant_handler(lambda b: False)))
    assert "No configuration answered" in report["verdict"]
    assert "another model id" in report["verdict"]
    assert all(v["ok"] is False for v in report["variants"])
    assert all("gave_up_after" in v for v in report["variants"])


def test_the_probe_reports_both_modes_working_rather_than_inventing_a_cause():
    from t3_engine.ai_advisor.advisor import probe_variants

    report = probe_variants("sk-test", client=make_client(_variant_handler(lambda b: True)))
    assert "Both thinking modes work" in report["verdict"]
    assert "Thinking mode is the cause" not in report["verdict"]


# ---- models that cannot chat at all ----
# A different failure from a wrong id: these are real, working models of the
# wrong KIND. Pointing the analyst at one fails in a way that reads as a
# broken app rather than a wrong choice.

def test_an_embedding_model_is_refused_before_a_request_is_ever_sent():
    from t3_engine.ai_advisor.advisor import non_chat_reason

    reason = non_chat_reason("nvidia/llama-nemotron-embed-vl-1b-v2:free")
    assert reason is not None
    assert "embedding" in reason
    assert "no tool calling" in reason

    called = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(1)
        return chat_response("should never happen")

    with pytest.raises(AIAdvisorError, match="embedding"):
        request_commentary("sk-or-test", {"wave": "3"},
                           model="nvidia/llama-nemotron-embed-vl-1b-v2:free",
                           client=make_client(handler))
    assert not called          # no point spending a request on it


def test_an_ordinary_chat_model_is_not_refused():
    """The guard must not reject a working model over a substring."""
    from t3_engine.ai_advisor.advisor import non_chat_reason

    assert non_chat_reason("nvidia/nemotron-3-ultra-550b-a55b:free") is None
    assert non_chat_reason("moonshotai/kimi-k3") is None
    assert non_chat_reason("deepseek-ai/deepseek-v4-pro-0813") is None


def test_an_empty_model_id_says_how_to_pick_one():
    with pytest.raises(AIAdvisorError, match="press Diagnose"):
        request_commentary("sk-or-test", {"wave": "3"}, model="")


def test_the_catalogue_answers_whether_the_chosen_model_supports_tools():
    """The one fact that decides whether the AI Analyst can run, taken from
    the catalogue rather than guessed from the model's name."""
    from t3_engine.ai_advisor.advisor import check_access

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [
            {"id": "vendor/with-tools:free", "supported_parameters": ["tools", "temperature"]},
            {"id": "vendor/no-tools:free", "supported_parameters": ["temperature"]},
        ]})

    result = check_access("sk-or-test", model="vendor/with-tools:free", client=make_client(handler))
    assert result["tool_capable"] == ["vendor/with-tools:free"]
    assert result["tool_capable_count"] == 1
    assert result["model_supports_tools"] is True

    result = check_access("sk-or-test", model="vendor/no-tools:free", client=make_client(handler))
    assert result["model_supports_tools"] is False


def test_a_catalogue_without_capability_metadata_says_unknown_not_no():
    """Absent metadata is not evidence of absence - reporting "no tools"
    there would send the user chasing a problem that may not exist."""
    from t3_engine.ai_advisor.advisor import check_access

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "vendor/model"}]})

    result = check_access("sk-or-test", model="vendor/model", client=make_client(handler))
    assert result["model_supports_tools"] is None
    assert result["tool_capable_count"] == 0


# ---- busy providers, and not hand-swapping models because of them ----

def test_a_busy_upstream_is_retried_rather_than_reported_as_a_failure(monkeypatch):
    """"Service temporarily overloaded" arrived from NVIDIA through
    OpenRouter inside a 200. It is a wait, exactly like a 429 - failing the
    whole run over a queue that clears in seconds is what sent a user
    swapping models by hand."""
    monkeypatch.setattr("t3_engine.ai_advisor.advisor._sleep", lambda s: None)
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(200, text=sse(
                {"error": {"message": "Error from Nvidia: Service temporarily overloaded"}}))
        return chat_response("recovered")

    result = request_commentary("sk-or-test", {"wave": "3"}, model="a/b", client=make_client(handler))
    assert result.text == "recovered"
    assert len(attempts) == 3


def test_a_persistent_overload_finally_reports_it_with_the_fix(monkeypatch):
    monkeypatch.setattr("t3_engine.ai_advisor.advisor._sleep", lambda s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse(
            {"error": {"message": "Error from Nvidia: Service temporarily overloaded"}}))

    with pytest.raises(AIAdvisorError) as excinfo:
        request_commentary("sk-or-test", {"wave": "3"}, model="a/b", client=make_client(handler))
    message = str(excinfo.value)
    assert "temporarily overloaded" in message      # the vendor's own words
    assert "busy rather than broken" in message
    assert "comma-separated" in message             # and what to do about it


def test_a_5xx_is_retried_too_since_a_saturated_vendor_usually_recovers(monkeypatch):
    monkeypatch.setattr("t3_engine.ai_advisor.advisor._sleep", lambda s: None)
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return chat_response("ok") if len(attempts) > 2 else httpx.Response(503, text="upstream down")

    result = request_commentary("sk-or-test", {"wave": "3"}, model="a/b", client=make_client(handler))
    assert result.text == "ok"
    assert len(attempts) == 3


def test_a_non_transient_error_is_not_retried(monkeypatch):
    """Retrying a bad key three times just makes the wrong answer slower."""
    monkeypatch.setattr("t3_engine.ai_advisor.advisor._sleep", lambda s: None)
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(200, text=sse({"error": {"message": "invalid model id"}}))

    with pytest.raises(AIAdvisorError, match="invalid model id"):
        request_commentary("sk-or-test", {"wave": "3"}, model="a/b", client=make_client(handler))
    assert len(attempts) == 1


# ---- fallback lists ----

def test_the_model_field_accepts_a_list_and_the_router_walks_it():
    """A single free model behind a shared GPU pool is unavailable a good
    fraction of the time. Swapping ids by hand is the thing a router exists
    to do instead."""
    from t3_engine.ai_advisor.advisor import parse_model_list

    assert parse_model_list("a/one, b/two,c/three") == ["a/one", "b/two", "c/three"]
    assert parse_model_list("a/one\nb/two") == ["a/one", "b/two"]
    assert parse_model_list(" a/one , a/one ") == ["a/one"]       # deduped
    assert parse_model_list("  ") == []

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return chat_response("ok")

    request_commentary("sk-or-test", {"wave": "3"}, model="a/one, b/two, c/three",
                       client=make_client(handler))
    assert seen["model"] == "a/one"                  # the preferred one
    assert seen["models"] == ["a/one", "b/two", "c/three"]


def test_a_single_model_does_not_get_a_fallback_array():
    """An array of one is noise, and a parameter a stricter provider could
    reject for no benefit."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return chat_response("ok")

    request_commentary("sk-or-test", {"wave": "3"}, model="a/one", client=make_client(handler))
    assert "models" not in seen


def test_every_entry_in_a_fallback_list_is_checked_for_being_a_chat_model():
    """A wrong-kind model sitting third would otherwise surface only once
    the first two were busy - the worst possible moment to learn about it."""
    with pytest.raises(AIAdvisorError, match="embedding"):
        request_commentary("sk-or-test", {"wave": "3"},
                           model="good/chat, other/chat, nvidia/llama-nemotron-embed-vl-1b-v2:free")
