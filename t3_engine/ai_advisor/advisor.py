"""Optional Gemini advisory layer + AI wave-labelling prompt builder.

Two separate jobs, deliberately kept apart because they carry very
different amounts of trust:

  `request_commentary` - a SECOND OPINION, never a decision-maker. The
  whole spec (sections 1, 5, 7, 22, 23) is built around Elliott hard rules
  and the weighted confidence score being what accept or reject a trade.
  This returns prose a human reads; the engine never calls it on its own
  entry path.

  `request_wave_count` - asks the model to propose a count over the WHOLE
  loaded history, which is the one thing the deterministic engine
  deliberately does not do (it only anchors on recent pivots). The model's
  answer is untrusted structured data: it names pivot INDICES, never
  prices or labels the server hasn't already confirmed, and every answer
  goes through elliott_engine/external_count.py before it can reach a
  chart or a trade. The model picks which pivots to connect; the server
  decides what is a legal wave.

PROVIDER: Google AI Studio (Gemini). This replaced OpenAI on request. BYO
key throughout - the key is accepted per request, forwarded once, and
never written to disk, DB or logs. See dashboard/server.py's
`/api/ai/advice` and `/api/ai/label` endpoints.

NETWORK NOTE: same situation as market_data/ - this sandbox blocks
outbound access to generativelanguage.googleapis.com as well. The
request-building and response-parsing below is real and unit-tested
against a mocked HTTP transport (tests/test_ai_advisor.py); it has not
completed a real call in this session. Everything downstream of it -
validation, chart rendering, trade evaluation - is exercised end to end
offline with recorded model responses, so a missing key degrades the
feature without breaking the pipeline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

DEFAULT_MODEL = "gemini-2.5-flash"
API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

ADVISOR_SYSTEM_PROMPT = (
    "You are a risk-aware trading assistant reviewing an Elliott Wave signal that has "
    "ALREADY been accepted or rejected by a rule-based engine. You cannot change that "
    "decision. Give a short, concrete second opinion: what would make you doubt this "
    "count, what to watch for next, and whether the risk/reward looks reasonable. Be "
    "skeptical and specific - do not just agree. Answer in under 150 words."
)

LABELLER_SYSTEM_PROMPT = (
    "You are an Elliott Wave analyst. You are given a list of CONFIRMED swing pivots, "
    "each with an index, a timestamp, a price and a kind (HIGH or LOW), in chronological "
    "order. Propose the single best wave count over this history.\n\n"
    "Rules you must follow, because the server re-checks every one of them and will "
    "reject your answer otherwise:\n"
    "- Refer to pivots ONLY by their given index. Never invent an index.\n"
    "- Label waves in canonical order starting at 1: 1,2,3,4,5 and then optionally A,B,C. "
    "Do not skip or reorder labels. Only count A,B,C if a full 1-5 precedes them.\n"
    "- Each wave runs from one pivot to a LATER pivot, and each wave must start exactly "
    "where the previous wave ended.\n"
    "- Each wave must connect a HIGH to a LOW or a LOW to a HIGH.\n"
    "- Wave 2 never retraces beyond the start of wave 1. Wave 3 is never the shortest of "
    "waves 1, 3 and 5. Wave 4 never enters wave 1's price territory.\n\n"
    "Answer with JSON only: {\"waves\": [{\"label\": \"1\", \"start_pivot_index\": 0, "
    "\"end_pivot_index\": 4}, ...], \"reasoning\": \"one or two sentences\"}"
)


class AIAdvisorError(Exception):
    pass


@dataclass
class AdvisorResponse:
    text: str
    model: str
    raw: Dict[str, Any]


@dataclass
class WaveCountProposal:
    waves: List[Dict[str, Any]]
    reasoning: str
    model: str
    raw: Dict[str, Any]


def build_contents(system_prompt: str, user_content: str) -> Dict[str, Any]:
    """Gemini's generateContent shape. The instruction goes in
    `systemInstruction` rather than being glued onto the user turn, so the
    model treats the pivot data as data - not as further instructions."""
    return {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_content}]}],
    }


def _post(api_key: str, model: str, payload: Dict[str, Any],
          client: Optional[httpx.Client], timeout: float) -> Dict[str, Any]:
    if not api_key:
        raise AIAdvisorError("No Gemini API key provided (get one free at https://aistudio.google.com/apikey)")

    http_client = client or httpx.Client(timeout=timeout)
    owns_client = client is None
    try:
        resp = http_client.post(
            f"{API_BASE}/{model}:generateContent",
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
        )
        if resp.status_code != 200:
            raise AIAdvisorError(f"Gemini API error {resp.status_code}: {resp.text[:300]}")
        return resp.json()
    except httpx.RequestError as exc:
        raise AIAdvisorError(f"Could not reach the Gemini API: {exc}")
    finally:
        if owns_client:
            http_client.close()


def _extract_text(data: Dict[str, Any]) -> str:
    """Pull the text out of a generateContent response, failing loudly
    rather than returning an empty string - a silently blank answer in a
    trading tool reads as "the model had no concerns", which is the exact
    opposite of "the call did not work"."""
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        blocked = (data.get("promptFeedback") or {}).get("blockReason")
        if blocked:
            raise AIAdvisorError(f"Gemini refused to answer (blockReason: {blocked})")
        raise AIAdvisorError(f"Unexpected Gemini response shape: {json.dumps(data)[:300]}")
    text = "".join(part.get("text", "") for part in parts).strip()
    if not text:
        raise AIAdvisorError("Gemini returned an empty answer")
    return text


def request_commentary(api_key: str, context: Dict[str, Any], model: str = DEFAULT_MODEL,
                        client: Optional[httpx.Client] = None, timeout: float = 20.0) -> AdvisorResponse:
    user_content = (
        "Here is the current Elliott Wave engine state as JSON. Give your second opinion.\n\n"
        + json.dumps(context, indent=2, default=str)
    )
    payload = build_contents(ADVISOR_SYSTEM_PROMPT, user_content)
    payload["generationConfig"] = {"temperature": 0.4, "maxOutputTokens": 400}
    data = _post(api_key, model, payload, client, timeout)
    return AdvisorResponse(text=_extract_text(data), model=model, raw=data)


def _parse_count_json(text: str) -> Dict[str, Any]:
    """Models wrap JSON in ``` fences even when told not to, so strip them
    before parsing rather than failing the whole call over formatting."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned
        cleaned = cleaned.rsplit("```", 1)[0]
        if cleaned.lstrip().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise AIAdvisorError(f"Gemini did not return valid JSON: {exc}. Raw answer: {text[:300]}")
    if not isinstance(parsed, dict):
        raise AIAdvisorError(f"Gemini returned {type(parsed).__name__}, expected a JSON object")
    return parsed


def request_wave_count(api_key: str, pivots: List[Dict[str, Any]], direction: str,
                        model: str = DEFAULT_MODEL, client: Optional[httpx.Client] = None,
                        timeout: float = 30.0) -> WaveCountProposal:
    """Ask for a count over the whole pivot history. The returned `waves`
    are RAW model output - structurally unvalidated on purpose. Pass them
    straight to elliott_engine.external_count.validate_external_count;
    never render or trade them directly."""
    user_content = (
        f"Prevailing trend direction: {direction}.\n"
        f"Confirmed swing pivots ({len(pivots)} of them), oldest first:\n"
        + json.dumps(pivots, separators=(",", ":"))
    )
    payload = build_contents(LABELLER_SYSTEM_PROMPT, user_content)
    payload["generationConfig"] = {
        "temperature": 0.1,          # a count is an analysis, not a creative task
        "maxOutputTokens": 2048,
        "responseMimeType": "application/json",
    }
    data = _post(api_key, model, payload, client, timeout)
    parsed = _parse_count_json(_extract_text(data))
    waves = parsed.get("waves")
    if waves is None:
        raise AIAdvisorError(f"Gemini's JSON has no 'waves' key: {json.dumps(parsed)[:300]}")
    return WaveCountProposal(
        waves=waves,
        reasoning=str(parsed.get("reasoning", "")),
        model=model,
        raw=data,
    )
