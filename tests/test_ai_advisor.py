"""NVIDIA API Catalog client tests.

Every one runs against a mocked transport - this sandbox has no outbound
access to integrate.api.nvidia.com, and more importantly the whole point of
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
        assert str(request.url) == "https://integrate.api.nvidia.com/v1/chat/completions"
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
    with pytest.raises(AIAdvisorError, match="No NVIDIA API Catalog API key"):
        request_commentary("", {"wave": "3"})


def test_request_commentary_raises_on_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="invalid request")

    with pytest.raises(AIAdvisorError, match="NVIDIA API Catalog API error 400"):
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


def test_auth_credit_and_rate_limit_errors_point_at_the_right_cause():
    """Three different failures that all look like "the AI is broken" from
    the outside, and have three completely different fixes."""
    cases = [
        (403, "permission denied", "Check the API key itself"),
        (400, "unknown parameter", "rejected parameter"),
        (429, "rate limited", "Rate limited"),
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

    request_commentary("sk-test", {"wave": "3"}, base_url="https://integrate.api.nvidia.com/v2",
                       client=make_client(handler))
    assert seen["url"] == "https://integrate.api.nvidia.com/v2/chat/completions"


def test_a_pasted_full_endpoint_is_not_doubled_up():
    """People paste the whole endpoint they see in a docs page far more
    often than they paste a bare base, and '/chat/completions/chat/
    completions' is a confusing 404 to debug."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return chat_response("ok")

    request_commentary("sk-test", {"wave": "3"},
                       base_url="https://integrate.api.nvidia.com/v1/chat/completions/",
                       client=make_client(handler))
    assert seen["url"] == "https://integrate.api.nvidia.com/v1/chat/completions"


def test_the_model_id_actually_reaches_the_request_body():
    """The Model field is only useful if it's what gets called. On a router
    the model is a body field, not part of the URL."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return chat_response("ok")

    request_commentary("sk-test", {"wave": "3"}, model="moonshotai/kimi-k3",
                       client=make_client(handler))
    assert seen["model"] == "moonshotai/kimi-k3"


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


def test_ping_confirms_the_key_url_and_model_in_one_tiny_request():
    """A wrong key, a wrong URL, a dead model id and a model that merely
    queues all look identical from the dashboard. This separates them."""
    from t3_engine.ai_advisor.advisor import ping

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        assert seen["stream"] is False        # a config check must not also test the stream path
        return plain_response("ok")

    result = ping("sk-test", model="moonshotai/kimi-k3", client=make_client(handler))
    assert result["ok"] is True
    assert result["answer"] == "ok"
    assert result["endpoint"].endswith("/chat/completions")
    # Tiny on purpose: this must not itself be slow enough to time out.
    assert seen["max_tokens"] <= 16


# ---- wave-count proposals ----

SAMPLE_PIVOTS = [
    {"index": 0, "time": 0, "price": 100.0, "kind": "LOW"},
    {"index": 1, "time": 60, "price": 150.0, "kind": "HIGH"},
]


def test_request_wave_count_parses_structured_json():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        # A count is an analysis, not a creative task.
        assert body["temperature"] <= 0.2
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

def test_seed_is_sent_so_the_same_chart_gives_the_same_count():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return chat_response("ok")

    request_commentary("sk-test", {"wave": "3"}, client=make_client(handler))
    assert seen["seed"] == 0


def test_reasoning_effort_is_absent_unless_asked_for():
    """An unsupported parameter is a 400, not a graceful ignore, and the
    endpoint is user-editable - so an unset field must be an ABSENT field
    rather than a default value."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return chat_response("ok")

    request_commentary("sk-test", {"wave": "3"}, client=make_client(handler))
    assert "reasoning_effort" not in seen

    seen.clear()
    request_commentary("sk-test", {"wave": "3"}, reasoning_effort="max", client=make_client(handler))
    assert seen["reasoning_effort"] == "max"


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
