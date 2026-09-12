"""Optional GPT advisory layer ("подключить мой купленный ChatGPT").

IMPORTANT - this is deliberately a SECOND OPINION, not a decision-maker:
the whole spec (sections 1, 5, 7, 22, 23) is built around the idea that
Elliott hard rules and the weighted confidence score are what accept or
reject a trade. Nothing here is allowed to override that - `get_commentary`
never returns a trade decision, only a text critique of a signal the
engine has ALREADY produced. If you want GPT's opinion to matter, read it
and decide manually; the engine itself never calls this module on its own
entry path.

BYO key: the user supplies their own OpenAI API key (they said "мой
купленный ChatGPT" - their own subscription/credits). The backend never
stores it - it is accepted per-request and forwarded to OpenAI, exactly
like a proxy. See dashboard/server.py's `/api/ai/advice` endpoint.

NETWORK NOTE: same situation as market_data/ - this sandbox blocks
outbound access to api.openai.com too (confirmed, same 403-at-proxy
pattern). The request-building and response-parsing logic below is real
and unit-tested against a mocked HTTP transport
(tests/test_ai_advisor.py); it has not completed a real call against
OpenAI in this session. It will work as soon as it runs somewhere with
normal internet access and a valid key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

DEFAULT_MODEL = "gpt-4o-mini"
API_URL = "https://api.openai.com/v1/chat/completions"

SYSTEM_PROMPT = (
    "You are a risk-aware trading assistant reviewing an Elliott Wave signal that has "
    "ALREADY been accepted or rejected by a rule-based engine. You cannot change that "
    "decision. Give a short, concrete second opinion: what would make you doubt this "
    "count, what to watch for next, and whether the risk/reward looks reasonable. Be "
    "skeptical and specific - do not just agree. Answer in under 150 words."
)


class AIAdvisorError(Exception):
    pass


@dataclass
class AdvisorResponse:
    text: str
    model: str
    raw: Dict[str, Any]


def build_messages(context: Dict[str, Any]) -> list:
    user_content = (
        "Here is the current Elliott Wave engine state as JSON. Give your second opinion.\n\n"
        + json.dumps(context, indent=2, default=str)
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def request_commentary(api_key: str, context: Dict[str, Any], model: str = DEFAULT_MODEL,
                        client: Optional[httpx.Client] = None, timeout: float = 20.0) -> AdvisorResponse:
    if not api_key:
        raise AIAdvisorError("No OpenAI API key provided")

    http_client = client or httpx.Client(timeout=timeout)
    owns_client = client is None
    try:
        resp = http_client.post(
            API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": build_messages(context), "temperature": 0.4, "max_tokens": 300},
        )
        if resp.status_code != 200:
            raise AIAdvisorError(f"OpenAI API error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        return AdvisorResponse(text=text, model=model, raw=data)
    finally:
        if owns_client:
            http_client.close()
