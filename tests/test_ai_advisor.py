"""Gemini (Google AI Studio) client tests.

Every one runs against a mocked transport - this sandbox has no outbound
access to generativelanguage.googleapis.com, and more importantly the
whole point of the design is that nothing downstream depends on a live
model call succeeding."""

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


def gemini_text_response(text: str) -> httpx.Response:
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})


def test_request_commentary_parses_gemini_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == "AIza-test"
        assert "generativelanguage.googleapis.com" in str(request.url)
        assert ":generateContent" in str(request.url)
        return gemini_text_response("This wave 3 count looks reasonable but watch for extension.")

    result = request_commentary("AIza-test", {"wave": "3", "confidence": 82}, client=make_client(handler))
    assert "wave 3" in result.text.lower()


def test_commentary_sends_the_instruction_as_system_not_as_a_user_turn():
    """Pivot data and engine state are DATA. Keeping the instruction in
    systemInstruction is what stops a number in the payload from reading
    as a new instruction to the model."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return gemini_text_response("ok")

    request_commentary("AIza-test", {"wave": "3"}, client=make_client(handler))
    assert "systemInstruction" in seen
    assert seen["contents"][0]["role"] == "user"


def test_request_commentary_raises_without_key():
    with pytest.raises(AIAdvisorError, match="No Gemini API key"):
        request_commentary("", {"wave": "3"})


def test_request_commentary_raises_on_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="API key not valid")

    with pytest.raises(AIAdvisorError, match="Gemini API error 400"):
        request_commentary("AIza-bad", {"wave": "3"}, client=make_client(handler))


def test_blocked_prompt_surfaces_as_an_error_not_an_empty_answer():
    """A blank second opinion reads as "no concerns", which is the
    opposite of "the call did not work"."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})

    with pytest.raises(AIAdvisorError, match="blockReason: SAFETY"):
        request_commentary("AIza-test", {"wave": "3"}, client=make_client(handler))


def test_empty_candidate_text_is_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return gemini_text_response("   ")

    with pytest.raises(AIAdvisorError, match="empty answer"):
        request_commentary("AIza-test", {"wave": "3"}, client=make_client(handler))


def test_network_failure_is_reported_clearly():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    with pytest.raises(AIAdvisorError, match="Could not reach the Gemini API"):
        request_commentary("AIza-test", {"wave": "3"}, client=make_client(handler))


# ---- wave-count proposals ----

SAMPLE_PIVOTS = [
    {"index": 0, "time": 0, "price": 100.0, "kind": "LOW"},
    {"index": 1, "time": 60, "price": 150.0, "kind": "HIGH"},
]


def test_request_wave_count_parses_structured_json():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        # A count is an analysis, not a creative task.
        assert body["generationConfig"]["temperature"] <= 0.2
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        return gemini_text_response(json.dumps({
            "waves": [{"label": "1", "start_pivot_index": 0, "end_pivot_index": 1}],
            "reasoning": "Clean impulse off the low.",
        }))

    proposal = request_wave_count("AIza-test", SAMPLE_PIVOTS, "UP", client=make_client(handler))
    assert proposal.waves == [{"label": "1", "start_pivot_index": 0, "end_pivot_index": 1}]
    assert "impulse" in proposal.reasoning


def test_request_wave_count_tolerates_markdown_fenced_json():
    """Models wrap JSON in ``` fences even when told not to - that's a
    formatting quirk, not a reason to throw the answer away."""
    fenced = '```json\n{"waves": [{"label": "1", "start_pivot_index": 0, "end_pivot_index": 1}]}\n```'

    def handler(request: httpx.Request) -> httpx.Response:
        return gemini_text_response(fenced)

    proposal = request_wave_count("AIza-test", SAMPLE_PIVOTS, "UP", client=make_client(handler))
    assert proposal.waves[0]["label"] == "1"


def test_request_wave_count_rejects_non_json_prose():
    def handler(request: httpx.Request) -> httpx.Response:
        return gemini_text_response("I think this is a wave 3, roughly speaking.")

    with pytest.raises(AIAdvisorError, match="did not return valid JSON"):
        request_wave_count("AIza-test", SAMPLE_PIVOTS, "UP", client=make_client(handler))


def test_request_wave_count_rejects_json_without_a_waves_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return gemini_text_response(json.dumps({"analysis": "looks bullish"}))

    with pytest.raises(AIAdvisorError, match="no 'waves' key"):
        request_wave_count("AIza-test", SAMPLE_PIVOTS, "UP", client=make_client(handler))
