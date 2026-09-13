"""Optional NVIDIA-API-Catalog advisory layer + AI wave-labelling prompt builder.

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

PROVIDER: the NVIDIA API Catalog (https://integrate.api.nvidia.com/v1),
running moonshotai/kimi-k3 by default. This is the fourth provider this
module has been pointed at (OpenAI -> Gemini -> OrcaRouter -> here), which
is precisely why every provider-specific value - endpoint, model id,
timeout, reasoning effort - is a field rather than a constant. Nothing
downstream of this module cares which model answered.

The wire format is OpenAI-compatible chat completions, so the payloads
here are plain `messages` + `tools`.

STREAMING: chat calls stream by default (`stream: true`, SSE), and the
chunks are reassembled into the ordinary non-streaming response shape
before anything else sees them - so the rest of the codebase is unchanged.
This is not cosmetic. A heavy reasoning model can think for minutes before
its first visible token, and a non-streaming request spends that whole time
with a silent socket, which production hit as a read timeout that threw
away the entire run. A streaming connection delivers bytes as they are
produced, so the read clock keeps being reset and the wait is bounded by
the whole-run budget instead of by one silent gap. (If a provider buffers
its reasoning and sends nothing until the end, streaming cannot help - the
configurable timeout still covers that case.)

ENDPOINT: the sandbox this was written in cannot reach
integrate.api.nvidia.com either (the egress proxy refuses the CONNECT), so
the default below is the URL from NVIDIA's own API-catalog snippet rather
than something confirmed from here. It stays overridable per request and
by environment variable, the same way the model id is.

BYO key throughout - the key is accepted per request, forwarded once, and
never written to disk, DB or logs. See dashboard/server.py's
`/api/ai/advice`, `/api/ai/label` and `/api/ai/analyst` endpoints.

NETWORK NOTE: same situation as market_data/ - this sandbox blocks
outbound access to integrate.api.nvidia.com as well. The request-building and
response-parsing below is real and unit-tested against a mocked HTTP
transport (tests/test_ai_advisor.py); it has not completed a real call in
this session. Everything downstream of it - validation, chart rendering,
trade evaluation - is exercised end to end offline with recorded model
responses, so a missing key degrades the feature without breaking the
pipeline.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

# NVIDIA API Catalog model ids are "<vendor>/<model>". This one is the
# default because it is what was asked for; it is not authoritative, so the
# dashboard keeps it editable and surfaces the API's own error verbatim
# when an id stops resolving.
DEFAULT_MODEL = "moonshotai/kimi-k3"
DEFAULT_API_BASE = os.getenv("T3_AI_API_BASE", "https://integrate.api.nvidia.com/v1")
PROVIDER_NAME = "NVIDIA API Catalog"

# A server-side key, so the dashboard works without pasting one into the
# browser. Read from the environment ONLY - never a literal in this file,
# because a secret committed to a repo stays in its history forever even
# after it is deleted.
#
# Worth knowing before setting it: this dashboard has no login. A key here
# is usable by anyone who can open the page, which turns the app into an
# open proxy to whoever owns the key. A per-browser key (the AI tab field)
# does not have that property. The request's own key always wins, so the
# two can coexist.
DEFAULT_API_KEY = os.getenv("T3_AI_API_KEY", "")


def resolve_api_key(supplied: Optional[str]) -> str:
    """The caller's key if there is one, else the server's."""
    return (supplied or "").strip() or DEFAULT_API_KEY

# Reasoning effort. NVIDIA's own snippet for this model uses "max"; that is
# the best answer and the slowest one, and the analyst makes up to
# max_steps sequential calls, so the cost multiplies. Left unset by default
# (the parameter is simply not sent) because an unsupported parameter is a
# 400 on a provider that does not know it - the UI offers it explicitly.
VALID_REASONING_EFFORTS = ("low", "medium", "high", "max")

# A fixed seed makes the same chart produce the same count, which is worth
# having for an analysis tool: two runs that disagree should mean the data
# changed, not that the sampler rolled differently. Not a guarantee - it is
# best-effort on every provider that offers it.
DEFAULT_SEED = 0

# Output-token budgets. Under Gemini these were 400 and 2048, which covered
# the answers themselves but NOT the internal reasoning current models emit
# first - so in production the second opinion arrived as half a sentence
# and the wave count as "Unterminated string ... char 169". The budget is
# now sized for reasoning plus answer, and a truncation that still happens
# is reported as a truncation (see _extract_text) instead of as a JSON
# parse error that sends you looking at the wrong thing.
COMMENTARY_MAX_OUTPUT_TOKENS = 2048
COUNT_MAX_OUTPUT_TOKENS = 16384

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
# Rate limiting is the dominant failure on a free tier, and it is a WAIT,
# not a defect: the run was going fine and the quota window simply closed.
# Retrying it inside one call is what turns "the analyst died at step 5"
# into "the analyst paused at step 5". Bounded, because a 429 that never
# clears must not hold a request open forever.
MAX_RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFF_SECONDS = (4.0, 12.0, 30.0)
MAX_RETRY_AFTER_SECONDS = 60.0

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
        hint = (f" | Either the model id '{model}' does not exist in this catalogue, or the endpoint "
                "is wrong - both come back as 404. Both are editable in the AI tab (the Model field "
                "and the API base URL field), so neither needs a redeploy.")
    elif resp.status_code in (401, 403):
        hint = (" | Check the API key itself - it may be invalid, revoked, or without access to this "
                "model.")
    elif resp.status_code == 400:
        hint = (" | A rejected parameter is the usual cause here. Reasoning effort and seed are sent "
                "only when set, so try clearing them in the AI tab if this model does not accept "
                "them.")
    elif resp.status_code == 402:
        hint = " | Out of credits for this model."
    elif resp.status_code == 429:
        hint = (f" | Rate limited, and still rate limited after {MAX_RATE_LIMIT_RETRIES} automatic "
                "retries with backoff. A free tier's quota window is usually per-minute: wait a "
                "little, lower the step budget, or switch models in the Model field. Reasoning "
                "effort makes each step cost more, so 'max' hits this soonest.")
    return f"{PROVIDER_NAME} API error {resp.status_code}: {detail}{hint}"


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


def _sleep(seconds: float) -> None:
    """Indirection so tests can exercise the retry policy without actually
    waiting 46 seconds for it."""
    time.sleep(seconds)


def _on_rate_limit(attempt: int, sleep_for: float) -> None:
    """A pause the user cannot see looks like a hang, so it goes to the
    log. Nothing here is a hard failure yet."""
    logging.getLogger(__name__).info(
        "Rate limited by %s; waiting %.0fs before retry %d/%d",
        PROVIDER_NAME, sleep_for, attempt + 1, MAX_RATE_LIMIT_RETRIES,
    )


def _headers(api_key: str, stream: bool) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }


def apply_model_options(payload: Dict[str, Any], seed: Optional[int] = DEFAULT_SEED,
                        reasoning_effort: Optional[str] = None) -> Dict[str, Any]:
    """Add the optional knobs, and ONLY when they are set.

    Both are provider-specific: sending `reasoning_effort` to a model that
    has never heard of it is a 400, not a graceful ignore, and the endpoint
    is user-editable so the next provider may well be such a model. An
    unset field is therefore an absent field, not a default value."""
    out = dict(payload)
    if seed is not None:
        out["seed"] = int(seed)
    if reasoning_effort:
        effort = str(reasoning_effort).strip().lower()
        if effort not in VALID_REASONING_EFFORTS:
            raise AIAdvisorError(
                f"reasoning_effort must be one of {', '.join(VALID_REASONING_EFFORTS)}, got {reasoning_effort!r}"
            )
        out["reasoning_effort"] = effort
    return out


def _merge_tool_call_deltas(accumulated: Dict[int, Dict[str, Any]], deltas: List[Dict[str, Any]]) -> None:
    """Tool calls arrive in FRAGMENTS over a stream: the id and name come in
    one chunk, then the arguments a few characters at a time, all keyed by
    `index`. Concatenating them in arrival order without keying on the
    index is how parallel tool calls get spliced into each other - which
    produces arguments that parse as valid JSON but describe a wave that
    was never proposed."""
    for delta in deltas or []:
        index = delta.get("index", 0)
        slot = accumulated.setdefault(index, {"id": "", "type": "function",
                                              "function": {"name": "", "arguments": ""}})
        if delta.get("id"):
            slot["id"] = delta["id"]
        if delta.get("type"):
            slot["type"] = delta["type"]
        function = delta.get("function") or {}
        if function.get("name"):
            slot["function"]["name"] += function["name"]
        if function.get("arguments"):
            slot["function"]["arguments"] += function["arguments"]


def _consume_stream(lines, model: str) -> Dict[str, Any]:
    """Reassemble an SSE stream into the ordinary non-streaming response
    shape, so nothing downstream needs to know how the bytes arrived."""
    content_parts: List[str] = []
    tool_calls: Dict[int, Dict[str, Any]] = {}
    finish_reason = ""
    saw_any_chunk = False

    for raw in lines:
        line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue          # keep-alives and comment frames are not fatal
        # An error can arrive mid-stream, after a 200 on the headers.
        if isinstance(chunk, dict) and chunk.get("error"):
            error = chunk["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise AIAdvisorError(f"{PROVIDER_NAME} returned an error for '{model}': {message}")
        choices = chunk.get("choices") or []
        if not choices:
            continue
        saw_any_chunk = True
        choice = choices[0]
        delta = choice.get("delta") or {}
        if delta.get("content"):
            content_parts.append(delta["content"])
        if delta.get("tool_calls"):
            _merge_tool_call_deltas(tool_calls, delta["tool_calls"])
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]

    if not saw_any_chunk:
        raise AIAdvisorError(
            f"{PROVIDER_NAME} opened a stream for '{model}' but sent no completion chunks."
        )

    message: Dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    return {"choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "model": model}


def retry_delay_for(resp: httpx.Response, attempt: int) -> float:
    """How long to wait before retrying a 429.

    The server's own Retry-After wins when it sends one - guessing shorter
    than the quota window just burns another attempt and can extend the
    ban. Capped, because some servers answer with an hour."""
    header = resp.headers.get("retry-after", "").strip()
    if header:
        try:
            return min(float(header), MAX_RETRY_AFTER_SECONDS)
        except ValueError:
            pass          # HTTP-date form; fall through to the backoff
    index = min(attempt, len(RATE_LIMIT_BACKOFF_SECONDS) - 1)
    return RATE_LIMIT_BACKOFF_SECONDS[index]


def _raise_for_error_body(data: Any, model: str) -> None:
    """An API can answer 200 with an error body. Treating that as a normal
    empty answer is how "the model had no concerns" gets printed when the
    call in fact failed."""
    if isinstance(data, dict) and data.get("error"):
        error = data["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise AIAdvisorError(f"{PROVIDER_NAME} returned an error for '{model}': {message}")


def _post(api_key: str, model: str, payload: Dict[str, Any],
          client: Optional[httpx.Client], timeout: float,
          base_url: Optional[str] = None, stream: bool = True) -> Dict[str, Any]:
    """One chat-completions call. Returns the non-streaming response shape
    whether or not the bytes arrived as a stream."""
    if not api_key:
        raise AIAdvisorError(f"No {PROVIDER_NAME} API key provided "
                             "(get one at https://build.nvidia.com)")

    url = chat_url(base_url)
    body = {**payload, "model": model, "stream": bool(stream)}
    http_client = client or httpx.Client(timeout=build_timeout(timeout))
    owns_client = client is None
    try:
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                if stream:
                    with http_client.stream("POST", url, headers=_headers(api_key, True),
                                            json=body) as resp:
                        if resp.status_code == 429 and attempt < MAX_RATE_LIMIT_RETRIES:
                            resp.read()
                            sleep_for = retry_delay_for(resp, attempt)
                            _on_rate_limit(attempt, sleep_for)
                            _sleep(sleep_for)
                            continue
                        if resp.status_code != 200:
                            resp.read()   # the body is not loaded yet in stream mode
                            raise AIAdvisorError(_api_error_message(resp, model))
                        return _consume_stream(resp.iter_lines(), model)
                    # unreachable, the `with` above either returns or raises
                resp = http_client.post(url, headers=_headers(api_key, False), json=body)
                if resp.status_code == 429 and attempt < MAX_RATE_LIMIT_RETRIES:
                    sleep_for = retry_delay_for(resp, attempt)
                    _on_rate_limit(attempt, sleep_for)
                    _sleep(sleep_for)
                    continue
                if resp.status_code != 200:
                    raise AIAdvisorError(_api_error_message(resp, model))
                data = resp.json()
            except httpx.RequestError as exc:
                raise AIAdvisorError(_transport_error_message(exc, url, timeout))
            _raise_for_error_body(data, model)
            return data
    finally:
        if owns_client:
            http_client.close()
    raise AIAdvisorError(  # pragma: no cover - the loop always returns or raises
        f"{PROVIDER_NAME} kept rate limiting '{model}' after {MAX_RATE_LIMIT_RETRIES} retries."
    )


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
                        base_url: Optional[str] = None, seed: Optional[int] = DEFAULT_SEED,
                        reasoning_effort: Optional[str] = None) -> AdvisorResponse:
    user_content = (
        "Here is the current Elliott Wave engine state as JSON. Give your second opinion.\n\n"
        + json.dumps(context, indent=2, default=str)
    )
    payload = apply_model_options({
        "messages": build_messages(ADVISOR_SYSTEM_PROMPT, user_content),
        "temperature": 0.4,
        "max_tokens": COMMENTARY_MAX_OUTPUT_TOKENS,
    }, seed=seed, reasoning_effort=reasoning_effort)
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
                        timeout: float = DEFAULT_READ_TIMEOUT, base_url: Optional[str] = None,
                        seed: Optional[int] = DEFAULT_SEED,
                        reasoning_effort: Optional[str] = None) -> WaveCountProposal:
    """Ask for a count over the whole pivot history. The returned `waves`
    are RAW model output - structurally unvalidated on purpose. Pass them
    straight to elliott_engine.external_count.validate_external_count;
    never render or trade them directly."""
    user_content = (
        f"Prevailing trend direction: {direction}.\n"
        f"Confirmed swing pivots ({len(pivots)} of them), oldest first:\n"
        + json.dumps(pivots, separators=(",", ":"))
    )
    payload = apply_model_options({
        "messages": build_messages(LABELLER_SYSTEM_PROMPT, user_content),
        "temperature": 0.1,          # a count is an analysis, not a creative task
        "max_tokens": COUNT_MAX_OUTPUT_TOKENS,
        "response_format": {"type": "json_object"},
    }, seed=seed, reasoning_effort=reasoning_effort)
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
    data = _post(api_key, model, payload, client, timeout, base_url, stream=False)
    message = _message_of(data)
    return {
        "ok": True,
        "model": data.get("model") or model,
        "endpoint": chat_url(base_url),
        "answer": (message.get("content") or "").strip()[:200],
        "finish_reason": _finish_reason(data),
    }
