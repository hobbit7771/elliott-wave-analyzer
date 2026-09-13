"""Optional OpenRouter advisory layer + AI wave-labelling prompt builder.

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

PROVIDER: OrcaRouter (https://orcarouter.ai). This replaced Google's
Gemini API on request, which had itself replaced OpenAI. The wire format
is OpenAI-compatible chat completions, so the payloads here are plain
`messages` + `tools` rather than Gemini's `contents`/`systemInstruction`/
`functionCall` shapes.

ENDPOINT: the sandbox this was written in cannot reach orcarouter.ai (the
egress proxy refuses the CONNECT), so the default below is not verified
here. It is, however, the URL that a production run reached successfully
at the transport level - see the comment on DEFAULT_API_BASE. It stays
overridable per request and by environment variable, the same way the
model id is: if it turns out wrong, that is a paste in the dashboard
rather than a redeploy.

The practical reason this keeps changing, and why the model name is an
editable field in the UI rather than a constant in this file: a router
carries hundreds of models from dozens of vendors, and which ones exist,
are free, or support tool calling changes week to week. Nothing downstream
of this module cares which model answered.

BYO key throughout - the key is accepted per request, forwarded once, and
never written to disk, DB or logs. See dashboard/server.py's
`/api/ai/advice`, `/api/ai/label` and `/api/ai/analyst` endpoints.

NETWORK NOTE: same situation as market_data/ - this sandbox blocks
outbound access to orcarouter.ai as well. The request-building and
response-parsing below is real and unit-tested against a mocked HTTP
transport (tests/test_ai_advisor.py); it has not completed a real call in
this session. Everything downstream of it - validation, chart rendering,
trade evaluation - is exercised end to end offline with recorded model
responses, so a missing key degrades the feature without breaking the
pipeline.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

# A router model id is "<vendor>/<model>", optionally with a ":free" or
# other variant suffix. This one is the default because it is what was
# asked for; it is NOT authoritative - OpenRouter's catalogue changes
# constantly, so the dashboard keeps the model name editable and surfaces
# the API's own error text verbatim when a name stops resolving.
DEFAULT_MODEL = "deepseek/deepseek-v4-flash-free"
# Default moved from https://orcarouter.ai/api/v1 to this after production
# evidence: a run against api.orcarouter.ai/v1 failed with a READ timeout,
# not a connect error or a 404. That means DNS resolved, TCP and TLS
# completed and the request was accepted - the host and path are live, and
# the model was simply still working. A wrong host fails at connect; a
# wrong path 404s immediately. Still overridable, for the same reason as
# before: nothing here has completed a real call.
DEFAULT_API_BASE = os.getenv("T3_AI_API_BASE", "https://api.orcarouter.ai/v1")

# Sent so the call is attributable on the router's side. Neither header is
# required, and neither carries anything about the user.
APP_TITLE = "T3 Elliott Wave Engine"
APP_URL = "https://github.com/hobbit7771/elliott-wave-analyzer"

# Output-token budgets. Under Gemini these were 400 and 2048, which covered
# the answers themselves but NOT the internal reasoning current models emit
# first - so in production the second opinion arrived as half a sentence
# and the wave count as "Unterminated string ... char 169". The budget is
# now sized for reasoning plus answer, and a truncation that still happens
# is reported as a truncation (see _extract_text) instead of as a JSON
# parse error that sends you looking at the wrong thing.
COMMENTARY_MAX_OUTPUT_TOKENS = 2048
COUNT_MAX_OUTPUT_TOKENS = 8192

# Timeouts, split by phase rather than one number for everything.
#
# Connecting is either fast or broken - 15s is already generous, and a short
# connect timeout is what makes "the host is wrong" fail quickly instead of
# looking like a slow model. READING is the slow part: a free router model
# queues behind other traffic, and a reasoning model thinks before it emits
# a first token, so a read can legitimately take minutes. Production hit
# exactly this - a read timeout at 120s on the analyst, reported as "could
# not reach the API", which is the wrong diagnosis: the connection worked
# fine.
CONNECT_TIMEOUT = 15.0
WRITE_TIMEOUT = 60.0
DEFAULT_READ_TIMEOUT = 180.0
MAX_READ_TIMEOUT = 600.0

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


TRUNCATED_MESSAGE = (
    "The model ran out of output tokens before finishing its answer (finish_reason: length). "
    "The answer was cut off mid-way - this is a budget problem, not a bad key or a bad model name."
)


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


def build_messages(system_prompt: str, user_content: str) -> List[Dict[str, Any]]:
    """Chat-completions shape. The instruction stays in its own `system`
    message rather than being glued onto the user turn, so the model treats
    the pivot data as data - not as further instructions."""
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


def chat_url(base_url: Optional[str] = None) -> str:
    """Resolve the chat-completions endpoint.

    Overridable at three levels - argument, T3_AI_API_BASE, hardcoded
    default - because the endpoint could not be verified from the build
    sandbox. A trailing '/chat/completions' in the supplied base is
    tolerated: people paste the full endpoint they see in a docs page far
    more often than they paste a bare base."""
    base = (base_url or DEFAULT_API_BASE).strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _api_error_message(resp: httpx.Response, model: str) -> str:
    """Pass the router's own error through verbatim, and add the one piece
    of context it can't know: that this app has a Model field you can edit.

    A router multiplexes many vendors, so the status code says which LAYER
    failed, and that is most of the diagnosis. 404 is almost always a model
    id that no longer resolves - free tiers get renamed or retired far more
    often than keys get revoked."""
    detail = resp.text[:400]
    hint = ""
    if resp.status_code == 404:
        hint = (f" | Either the model id '{model}' does not exist on OrcaRouter, or the API base URL "
                "is wrong - both come back as 404 and both are fixed in the AI tab (Model field / API "
                "base URL field), no redeploy needed. Check the id on orcarouter.ai's Models page.")
    elif resp.status_code in (401, 403):
        hint = (" | Check the API key itself - it may be invalid, revoked, or missing access to this "
                "model.")
    elif resp.status_code == 402:
        hint = (" | Out of credits for this model. Free models have their own hard rate limits; a paid "
                "model needs credit on the account.")
    elif resp.status_code == 429:
        hint = (" | Rate limited. Free models are throttled aggressively - wait, or switch to another "
                "model in the Model field.")
    return f"OrcaRouter API error {resp.status_code}: {detail}{hint}"


def build_timeout(read_seconds: float) -> httpx.Timeout:
    """Granular timeout. One scalar would apply the long read budget to the
    connect phase too, so an unreachable host would hang for minutes before
    admitting it."""
    read = max(10.0, min(float(read_seconds), MAX_READ_TIMEOUT))
    return httpx.Timeout(connect=CONNECT_TIMEOUT, read=read, write=WRITE_TIMEOUT, pool=read)


def _transport_error_message(exc: httpx.RequestError, url: str, read_seconds: float) -> str:
    """Name the phase that failed. "Could not reach the API" is true of a
    DNS or TCP failure and FALSE of a read timeout - there the connection
    worked and the model simply did not answer in time, which has a
    completely different fix (wait longer, smaller job, faster model)."""
    if isinstance(exc, httpx.ReadTimeout):
        return (f"The model did not answer within {read_seconds:.0f}s. The connection to {url} worked - "
                "this is the model taking too long, not an unreachable endpoint. Free router models "
                "queue behind other traffic. Raise the Timeout field, pick a faster model, or lower "
                "the step budget.")
    if isinstance(exc, httpx.ConnectTimeout):
        return (f"Could not open a connection to {url} within {CONNECT_TIMEOUT:.0f}s. Check the API "
                "base URL in the AI tab.")
    if isinstance(exc, httpx.ConnectError):
        return (f"Could not connect to {url}: {exc}. The host may be wrong or unreachable from this "
                "server - check the API base URL in the AI tab.")
    if isinstance(exc, httpx.WriteTimeout):
        return f"Timed out sending the request to {url}. The history may be too large."
    return f"Request to {url} failed: {exc}"


def _headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": APP_URL,
        "X-Title": APP_TITLE,
    }


def _post(api_key: str, model: str, payload: Dict[str, Any],
          client: Optional[httpx.Client], timeout: float,
          base_url: Optional[str] = None) -> Dict[str, Any]:
    if not api_key:
        raise AIAdvisorError("No OrcaRouter API key provided (get one at https://orcarouter.ai)")

    url = chat_url(base_url)
    body = {**payload, "model": model}
    http_client = client or httpx.Client(timeout=build_timeout(timeout))
    owns_client = client is None
    try:
        resp = http_client.post(url, headers=_headers(api_key), json=body)
        if resp.status_code != 200:
            raise AIAdvisorError(_api_error_message(resp, model))
        data = resp.json()
    except httpx.RequestError as exc:
        raise AIAdvisorError(_transport_error_message(exc, url, timeout))
    finally:
        if owns_client:
            http_client.close()

    # A router can answer 200 with an error body - an upstream vendor
    # refusing, a model being unavailable - and treating that as a normal
    # empty answer is how "the AI had no concerns" gets printed when the
    # call in fact failed.
    if isinstance(data, dict) and data.get("error"):
        error = data["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise AIAdvisorError(f"OrcaRouter returned an error for '{model}': {message}")
    return data


def _message_of(data: Dict[str, Any]) -> Dict[str, Any]:
    choices = data.get("choices") or []
    if not choices:
        raise AIAdvisorError(f"Unexpected OpenRouter response shape: {json.dumps(data)[:300]}")
    return choices[0].get("message") or {}


def _finish_reason(data: Dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    return str(choices[0].get("finish_reason") or choices[0].get("native_finish_reason") or "")


def _extract_text(data: Dict[str, Any]) -> str:
    """Pull the text out of a chat completion, failing loudly rather than
    returning an empty string - a silently blank answer in a trading tool
    reads as "the model had no concerns", which is the exact opposite of
    "the call did not work".

    A truncated answer is called out by name. Reasoning models spend output
    tokens on internal thinking before they emit anything visible, so a
    budget that looks generous for the answer alone can cut the answer off
    mid-sentence - which surfaced in production as half a sentence of
    commentary, and as "Unterminated string" when the answer was JSON. The
    cause is the token budget, not the model's formatting, and the message
    has to say so or the next person debugs the wrong thing."""
    message = _message_of(data)
    text = (message.get("content") or "").strip()
    if not text:
        if _finish_reason(data) == "length":
            raise AIAdvisorError(TRUNCATED_MESSAGE)
        raise AIAdvisorError("The model returned an empty answer")
    if _finish_reason(data) == "length":
        raise AIAdvisorError(f"{TRUNCATED_MESSAGE} Partial answer: {text[:300]}")
    return text


def request_commentary(api_key: str, context: Dict[str, Any], model: str = DEFAULT_MODEL,
                        client: Optional[httpx.Client] = None, timeout: float = DEFAULT_READ_TIMEOUT,
                        base_url: Optional[str] = None) -> AdvisorResponse:
    user_content = (
        "Here is the current Elliott Wave engine state as JSON. Give your second opinion.\n\n"
        + json.dumps(context, indent=2, default=str)
    )
    payload = {
        "messages": build_messages(ADVISOR_SYSTEM_PROMPT, user_content),
        "temperature": 0.4,
        "max_tokens": COMMENTARY_MAX_OUTPUT_TOKENS,
    }
    data = _post(api_key, model, payload, client, timeout, base_url)
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
        raise AIAdvisorError(f"The model did not return valid JSON: {exc}. Raw answer: {text[:300]}")
    if not isinstance(parsed, dict):
        raise AIAdvisorError(f"The model returned {type(parsed).__name__}, expected a JSON object")
    return parsed


def request_wave_count(api_key: str, pivots: List[Dict[str, Any]], direction: str,
                        model: str = DEFAULT_MODEL, client: Optional[httpx.Client] = None,
                        timeout: float = DEFAULT_READ_TIMEOUT, base_url: Optional[str] = None) -> WaveCountProposal:
    """Ask for a count over the whole pivot history. The returned `waves`
    are RAW model output - structurally unvalidated on purpose. Pass them
    straight to elliott_engine.external_count.validate_external_count;
    never render or trade them directly."""
    user_content = (
        f"Prevailing trend direction: {direction}.\n"
        f"Confirmed swing pivots ({len(pivots)} of them), oldest first:\n"
        + json.dumps(pivots, separators=(",", ":"))
    )
    payload = {
        "messages": build_messages(LABELLER_SYSTEM_PROMPT, user_content),
        "temperature": 0.1,          # a count is an analysis, not a creative task
        "max_tokens": COUNT_MAX_OUTPUT_TOKENS,
        "response_format": {"type": "json_object"},
    }
    data = _post(api_key, model, payload, client, timeout, base_url)
    parsed = _parse_count_json(_extract_text(data))
    waves = parsed.get("waves")
    if waves is None:
        raise AIAdvisorError(f"The model's JSON has no 'waves' key: {json.dumps(parsed)[:300]}")
    return WaveCountProposal(
        waves=waves,
        reasoning=str(parsed.get("reasoning", "")),
        model=model,
        raw=data,
    )


def ping(api_key: str, model: str = DEFAULT_MODEL, base_url: Optional[str] = None,
         client: Optional[httpx.Client] = None, timeout: float = 60.0) -> Dict[str, Any]:
    """One tiny round trip, to separate "the setup is wrong" from "this
    particular job is too slow".

    Worth its own function because those two failures look identical from
    the dashboard: a wrong key, a wrong URL, a retired model id and a model
    that simply queues for four minutes all present as "nothing happened".
    This asks for a single token, so anything other than a prompt answer is
    a configuration problem rather than a patience problem.

    It also reports whether the model advertises tool calling, which the AI
    Analyst requires and many free models lack."""
    payload = {
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        "max_tokens": 16,
        "temperature": 0,
    }
    data = _post(api_key, model, payload, client, timeout, base_url)
    message = _message_of(data)
    return {
        "ok": True,
        "model": data.get("model") or model,
        "endpoint": chat_url(base_url),
        "answer": (message.get("content") or "").strip()[:200],
        "finish_reason": _finish_reason(data),
    }
