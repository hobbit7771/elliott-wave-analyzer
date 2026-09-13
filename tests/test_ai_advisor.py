"""OrcaRouter client tests.

Every one runs against a mocked transport - this sandbox has no outbound
access to orcarouter.ai, and more importantly the whole point of the design
is that nothing downstream depends on a live model call succeeding."""

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


def chat_response(text: str, finish_reason: str = "stop") -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": finish_reason}],
    })


def test_request_commentary_parses_a_chat_completion():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-test"
        assert str(request.url) == "https://orcarouter.ai/api/v1/chat/completions"
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
    with pytest.raises(AIAdvisorError, match="No OrcaRouter API key"):
        request_commentary("", {"wave": "3"})


def test_request_commentary_raises_on_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="invalid request")

    with pytest.raises(AIAdvisorError, match="OrcaRouter API error 400"):
        request_commentary("sk-bad", {"wave": "3"}, client=make_client(handler))


def test_a_missing_model_404_is_passed_through_verbatim_with_a_usable_hint():
    """A router's catalogue churns constantly - free tiers get renamed and
    retired far more often than keys get revoked. The user needs BOTH
    halves: the API's message and where to act on it, so neither may be
    swallowed into a generic "AI unavailable"."""
    router_message = '{"error": {"code": 404, "message": "No endpoints found for deepseek/made-up-model."}}'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text=router_message)

    with pytest.raises(AIAdvisorError) as excinfo:
        request_commentary("sk-test", {"wave": "3"}, model="deepseek/made-up-model",
                           client=make_client(handler))
    message = str(excinfo.value)
    assert "No endpoints found" in message        # the router's own words survive
    assert "deepseek/made-up-model" in message
    assert "Model field" in message               # and where to act on it
    assert "API base URL" in message              # 404 has two causes here, both fixable in the UI


def test_auth_credit_and_rate_limit_errors_point_at_the_right_cause():
    """Three different failures that all look like "the AI is broken" from
    the outside, and have three completely different fixes."""
    cases = [
        (403, "permission denied", "Check the API key itself"),
        (402, "insufficient credits", "Out of credits"),
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

    request_commentary("sk-test", {"wave": "3"}, base_url="https://orcarouter.ai/v2",
                       client=make_client(handler))
    assert seen["url"] == "https://orcarouter.ai/v2/chat/completions"


def test_a_pasted_full_endpoint_is_not_doubled_up():
    """People paste the whole endpoint they see in a docs page far more
    often than they paste a bare base, and '/chat/completions/chat/
    completions' is a confusing 404 to debug."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return chat_response("ok")

    request_commentary("sk-test", {"wave": "3"},
                       base_url="https://orcarouter.ai/api/v1/chat/completions/",
                       client=make_client(handler))
    assert seen["url"] == "https://orcarouter.ai/api/v1/chat/completions"


def test_the_model_id_actually_reaches_the_request_body():
    """The Model field is only useful if it's what gets called. On a router
    the model is a body field, not part of the URL."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return chat_response("ok")

    request_commentary("sk-test", {"wave": "3"}, model="deepseek/deepseek-v4-flash-free",
                       client=make_client(handler))
    assert seen["model"] == "deepseek/deepseek-v4-flash-free"


def test_an_error_body_returned_with_http_200_is_still_an_error():
    """A router can answer 200 while the upstream vendor refused. Treating
    that as a normal empty answer is how "the AI had no concerns" gets
    printed when the call in fact failed."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"message": "upstream provider returned no completion"}})

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


def test_network_failure_is_reported_clearly():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    with pytest.raises(AIAdvisorError, match="Could not reach the OrcaRouter API"):
        request_commentary("sk-test", {"wave": "3"}, client=make_client(handler))


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
