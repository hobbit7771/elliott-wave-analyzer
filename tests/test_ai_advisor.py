import httpx
import pytest

from t3_engine.ai_advisor.advisor import AIAdvisorError, request_commentary


def make_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_request_commentary_parses_openai_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-test"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "This wave 3 count looks reasonable but watch for extension."}}]
        })

    client = make_client(handler)
    result = request_commentary("sk-test", {"wave": "3", "confidence": 82}, client=client)
    assert "wave 3" in result.text.lower()


def test_request_commentary_raises_without_key():
    with pytest.raises(AIAdvisorError):
        request_commentary("", {"wave": "3"})


def test_request_commentary_raises_on_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid api key")

    client = make_client(handler)
    with pytest.raises(AIAdvisorError):
        request_commentary("sk-bad", {"wave": "3"}, client=client)
