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
# Back on OpenRouter, which is the fifth provider this module has been
# pointed at. Every provider-specific value stays a field for that reason;
# none of them is worth a redeploy.
DEFAULT_MODEL = os.getenv("T3_AI_MODEL", "~openai/gpt-astra-latest")
DEFAULT_API_BASE = os.getenv("T3_AI_API_BASE", "https://openrouter.ai/api/v1")
PROVIDER_NAME = "OpenRouter"

# Model families that cannot serve a chat completion at all, matched on the
# id. This is not a guess about quality - an embedding model returns a
# vector from /v1/embeddings and has no text output or tool calling, so
# pointing the analyst at one fails in a way that looks like a broken app
# rather than like a wrong choice. Catching it by name turns a confusing
# 400 (or worse, a silent empty answer) into a sentence that says what the
# model is.
NON_CHAT_MODEL_MARKERS = ("embed", "embedding", "-rerank", "reranker", "moderation",
                          "whisper", "tts-", "-tts", "stable-diffusion", "flux")


def parse_model_list(raw: str) -> List[str]:
    """The Model field accepts a LIST, comma- or newline-separated.

    A single free model behind a shared GPU pool is offline or saturated a
    good fraction of the time, and the answer to that is not for a person
    to sit there swapping ids by hand - it is the thing a router exists to
    do. OpenRouter takes a `models` array and walks it in order when one
    fails, so the fallback happens inside the provider, in the same request,
    with no second round trip."""
    parts = [piece.strip() for chunk in (raw or "").replace("\n", ",").split(",")
             for piece in [chunk]]
    seen, models = set(), []
    for name in parts:
        if name and name not in seen:
            seen.add(name)
            models.append(name)
    return models


def non_chat_reason(model: str) -> Optional[str]:
    """Why this model id cannot answer a chat request, or None."""
    name = (model or "").lower()
    if any(marker in name for marker in NON_CHAT_MODEL_MARKERS):
        kind = "an embedding" if "embed" in name else "a non-chat"
        return (f"'{model}' looks like {kind} model. Those take input and return vectors or scores "
                "from a different endpoint - they have no text output and no tool calling, so they "
                "cannot give a second opinion, propose a wave count, or run the analyst. Pick a "
                "chat model; Diagnose lists the ones in your catalogue that support tool calling.")
    return None

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

# OpenRouter's own reasoning control: `reasoning: {enabled, effort}`. This
# replaced a NIM-specific `chat_template_kwargs: {thinking}`, which was the
# right field for models served directly by NVIDIA and the wrong one here -
# a router normalises reasoning across vendors and this is the field it
# normalises to.
#
# Default ON. It was off while the models were free ones behind a shared
# GPU pool, where thinking meant minutes to first byte; on a paid model
# thinking is the whole reason to use it, and an Elliott count is exactly
# the kind of work it helps. Still a switch.
DEFAULT_THINKING: Optional[bool] = True

# Sampling temperatures, chosen per job rather than one number everywhere.
#
# Set to 0.3 on the project owner's instruction. The argument for 0 is
# recorded here because it is the reason this was ever 0 and the trade-off
# is real: a wave count is not a creative task - the chart either does or
# does not contain a legal impulse - so at 0 the model always returns the
# count it rates highest, and two runs over the same candles agree.
#
# What 0.3 buys in exchange: the agent loop explores. It works the chart
# over a dozen-plus steps, and a little sampling lets it try a different
# degree or a different anchor instead of walking the same path to the same
# local answer every time. With every structure re-validated server-side,
# a worse count cannot reach the chart - it is rejected with the rule it
# broke - so the downside is a wasted step rather than a bad label.
#
# The cost is reproducibility: the same chart can now come back with
# different (still legal) counts. DEFAULT_SEED below is what pins that
# back down when it matters.
ANALYSIS_TEMPERATURE = 0.3
COMMENTARY_TEMPERATURE = 0.3

# Seed is NOT sent by default. It stays off because it is one more
# parameter a provider can reject with a 400, on an endpoint the user is
# free to point anywhere - but with ANALYSIS_TEMPERATURE above 0 it is now
# the only way to make a run repeatable, so set it when comparing two
# configurations against the same chart.
DEFAULT_SEED = None

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
# Phrases an upstream uses to say "busy, not broken". A free tier routed to
# a shared GPU pool hits these constantly, and they are a WAIT rather than a
# defect - the same category as a 429, and retried the same way. Matched on
# text because the status code does not distinguish them: OpenRouter
# forwards "Service temporarily overloaded" from the vendor inside a 200 or
# a 502 depending on where it failed.
TRANSIENT_UPSTREAM_MARKERS = ("overload", "temporarily", "capacity", "try again",
                              "unavailable", "timeout", "timed out", "busy",
                              "no instances", "queue is full",
                              # "Provider returned an empty response" - the
                              # upstream answered with nothing at all. Seen on
                              # a real run: nine paid steps of work, then this
                              # on the tenth, and the whole run was discarded
                              # for what is a one-off upstream hiccup. It
                              # belongs with the other retryables: the next
                              # attempt on the same conversation normally
                              # succeeds.
                              "empty response", "empty completion")

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


class TransientProviderError(AIAdvisorError):
    """The provider is busy, not broken. Worth retrying; not worth
    reporting as a failure until the retries are spent."""


def is_transient(message: str) -> bool:
    text = (message or "").lower()
    return any(marker in text for marker in TRANSIENT_UPSTREAM_MARKERS)


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
    elif resp.status_code == 202:
        hint = (" | 202 means the request was QUEUED, not answered: this endpoint expects the client "
                "to poll for the result rather than read a response. A plain chat client waits "
                "forever on that, which looks exactly like a slow model.")
    elif resp.status_code == 400:
        hint = (" | A rejected parameter is the usual cause here. Reasoning effort and seed are sent "
                "only when set, so try clearing them in the AI tab if this model does not accept "
                "them.")
    elif resp.status_code == 402:
        hint = " | Out of credits for this model."
    elif 500 <= resp.status_code < 600:
        hint = (f" | A 5xx from a router usually means the vendor behind this model is down or "
                f"saturated, and it survived {MAX_RATE_LIMIT_RETRIES} automatic retries. Put several "
                "model ids in the Model field, comma-separated: the router walks the list and uses "
                "the first one that answers.")
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
                        reasoning_effort: Optional[str] = None,
                        thinking: Optional[bool] = DEFAULT_THINKING,
                        top_p: Optional[float] = None) -> Dict[str, Any]:
    """Add the optional knobs, and ONLY when they are set.

    Both are provider-specific: sending `reasoning_effort` to a model that
    has never heard of it is a 400, not a graceful ignore, and the endpoint
    is user-editable so the next provider may well be such a model. An
    unset field is therefore an absent field, not a default value."""
    out = dict(payload)
    if seed is not None:
        out["seed"] = int(seed)
    reasoning: Dict[str, Any] = {}
    if reasoning_effort:
        effort = str(reasoning_effort).strip().lower()
        if effort not in VALID_REASONING_EFFORTS:
            raise AIAdvisorError(
                f"reasoning_effort must be one of {', '.join(VALID_REASONING_EFFORTS)}, got {reasoning_effort!r}"
            )
        reasoning["effort"] = effort
    if thinking is not None:
        reasoning["enabled"] = bool(thinking)
    if reasoning:
        out["reasoning"] = reasoning
    if top_p is not None:
        out["top_p"] = float(top_p)
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


def _merge_reasoning_details(accumulated: Dict[int, Dict[str, Any]],
                             deltas: List[Dict[str, Any]]) -> None:
    """Reassemble streamed reasoning blocks, keyed by index like tool calls.

    Text fields are concatenated; everything else (type, signature, ids,
    encrypted payloads) is taken as given and never altered - these are
    what let the provider resume the model's own reasoning, and a
    re-encoded blob is a broken one."""
    for delta in deltas or []:
        if not isinstance(delta, dict):
            continue
        index = delta.get("index", len(accumulated))
        slot = accumulated.setdefault(index, {})
        for key, value in delta.items():
            if key == "index":
                slot["index"] = value
            elif isinstance(value, str) and isinstance(slot.get(key), str):
                slot[key] += value
            else:
                slot[key] = value


def _consume_stream(lines, model: str) -> Dict[str, Any]:
    """Reassemble an SSE stream into the ordinary non-streaming response
    shape, so nothing downstream needs to know how the bytes arrived."""
    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    reasoning_details: Dict[int, Dict[str, Any]] = {}
    tool_calls: Dict[int, Dict[str, Any]] = {}
    finish_reason = ""
    saw_any_chunk = False
    # Token usage arrives in its own trailing chunk (one with no choices),
    # and was being dropped on the floor along with it. Without it there is
    # no way to answer "what does running this cost" with a number instead
    # of a guess - see the cost accounting in ai_advisor/usage.py.
    usage: Dict[str, Any] = {}

    # Kept so a provider that ignores `stream: true` and answers with an
    # ordinary JSON body still works. Without this the SSE reader waits out
    # the whole buffered generation and then reports an empty stream, which
    # blames the model for the gateway's behaviour.
    non_sse: List[str] = []

    for raw in lines:
        line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        line = line.strip()
        if not line:
            continue
        if not line.startswith("data:"):
            non_sse.append(line)
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
            _raise_for_error_body(chunk, model)
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices:
            continue
        saw_any_chunk = True
        choice = choices[0]
        delta = choice.get("delta") or {}
        if delta.get("content"):
            content_parts.append(delta["content"])
        # A reasoning model streams its thinking separately from its answer,
        # under a key the providers have not standardised. Capturing it is
        # what lets the dashboard show WHY a run went the way it did rather
        # than only what it ended up calling.
        for key in ("reasoning_content", "reasoning"):
            piece = delta.get(key)
            if isinstance(piece, str) and piece:
                reasoning_parts.append(piece)
                break
        if delta.get("tool_calls"):
            _merge_tool_call_deltas(tool_calls, delta["tool_calls"])
        if delta.get("reasoning_details"):
            _merge_reasoning_details(reasoning_details, delta["reasoning_details"])
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]

    if not saw_any_chunk:
        buffered = _as_buffered_completion("".join(non_sse), model)
        if buffered is not None:
            return buffered
        raise AIAdvisorError(
            f"{PROVIDER_NAME} opened a stream for '{model}' but sent no completion chunks."
        )

    message: Dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if reasoning_details:
        # Passed back UNMODIFIED on the next turn. A reasoning model picks
        # up where it left off from these, and the agent loop is many turns
        # long - dropping them makes every step start its thinking over,
        # which is both worse and more expensive. Some are signed or
        # encrypted blobs, so they are carried verbatim, never rebuilt.
        message["reasoning_details"] = [reasoning_details[i] for i in sorted(reasoning_details)]
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    out: Dict[str, Any] = {
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "model": model,
    }
    if usage:
        out["usage"] = usage
    return out


def _should_retry(resp: httpx.Response) -> bool:
    """429 is the classic case; 5xx from a router usually means the vendor
    behind it is down or saturated, which clears on its own far more often
    than it stays broken."""
    return resp.status_code == 429 or 500 <= resp.status_code < 600


def retry_delay_for_attempt(attempt: int) -> float:
    index = min(attempt, len(RATE_LIMIT_BACKOFF_SECONDS) - 1)
    return RATE_LIMIT_BACKOFF_SECONDS[index]


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


def _as_buffered_completion(body: str, model: str) -> Optional[Dict[str, Any]]:
    """A non-SSE body that is nonetheless a valid chat completion."""
    body = body.strip()
    if not body.startswith("{"):
        return None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    _raise_for_error_body(parsed, model)
    return parsed if parsed.get("choices") else None


def _raise_for_error_body(data: Any, model: str) -> None:
    """An API can answer 200 with an error body. Treating that as a normal
    empty answer is how "the model had no concerns" gets printed when the
    call in fact failed."""
    if isinstance(data, dict) and data.get("error"):
        error = data["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        # Some of these are the provider saying "busy", which is a wait
        # rather than a defect and must not end the call on the first try.
        failure = TransientProviderError if is_transient(str(message)) else AIAdvisorError
        suffix = ""
        if failure is TransientProviderError:
            suffix = (" | The provider is busy rather than broken. Put several model ids in the "
                      "Model field, comma-separated, and the router will use the first that "
                      "answers instead of failing the whole run.")
        raise failure(f"{PROVIDER_NAME} returned an error for '{model}': {message}{suffix}")


def _post(api_key: str, model: str, payload: Dict[str, Any],
          client: Optional[httpx.Client], timeout: float,
          base_url: Optional[str] = None, stream: bool = True) -> Dict[str, Any]:
    """One chat-completions call. Returns the non-streaming response shape
    whether or not the bytes arrived as a stream."""
    if not api_key:
        raise AIAdvisorError(f"No {PROVIDER_NAME} API key provided "
                             "(get one at https://build.nvidia.com)")

    models = parse_model_list(model)
    if not models:
        raise AIAdvisorError(
            "No model id set. Open the AI tab, press Diagnose to list the models your key can reach, "
            "and paste one that supports tool calling into the Model field."
        )
    # Every entry is checked, not just the first: a wrong-kind model sitting
    # third in a fallback list would only surface once the first two were
    # busy, which is the worst possible moment to learn about it.
    for name in models:
        problem = non_chat_reason(name)
        if problem:
            raise AIAdvisorError(problem)

    url = chat_url(base_url)
    body = {**payload, "model": models[0], "stream": bool(stream)}
    if stream:
        # Ask for the usage trailer explicitly. OpenAI-compatible providers
        # omit token counts from a stream unless this is set, and a run
        # whose cost is unknown cannot be budgeted - only guessed at.
        body["stream_options"] = {"include_usage": True}
    if len(models) > 1:
        body["models"] = models
    http_client = client or httpx.Client(timeout=build_timeout(timeout))
    owns_client = client is None
    try:
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                if stream:
                    with http_client.stream("POST", url, headers=_headers(api_key, True),
                                            json=body) as resp:
                        if _should_retry(resp) and attempt < MAX_RATE_LIMIT_RETRIES:
                            resp.read()
                            sleep_for = retry_delay_for(resp, attempt)
                            _on_rate_limit(attempt, sleep_for)
                            _sleep(sleep_for)
                            continue
                        if resp.status_code != 200:
                            resp.read()   # the body is not loaded yet in stream mode
                            raise AIAdvisorError(_api_error_message(resp, model))
                        try:
                            return _consume_stream(resp.iter_lines(), model)
                        except TransientProviderError:
                            # "Busy" can arrive as an error FRAME after a 200,
                            # which is how the streamed path - every chat call
                            # - would otherwise never retry a busy provider.
                            if attempt >= MAX_RATE_LIMIT_RETRIES:
                                raise
                            transient_wait = retry_delay_for_attempt(attempt)
                        _on_rate_limit(attempt, transient_wait)
                        _sleep(transient_wait)
                        continue
                    # unreachable, the `with` above either returns or raises
                resp = http_client.post(url, headers=_headers(api_key, False), json=body)
                if _should_retry(resp) and attempt < MAX_RATE_LIMIT_RETRIES:
                    sleep_for = retry_delay_for(resp, attempt)
                    _on_rate_limit(attempt, sleep_for)
                    _sleep(sleep_for)
                    continue
                if resp.status_code != 200:
                    raise AIAdvisorError(_api_error_message(resp, model))
                data = resp.json()
            except httpx.RequestError as exc:
                raise AIAdvisorError(_transport_error_message(exc, url, timeout))
            try:
                _raise_for_error_body(data, model)
            except TransientProviderError:
                # A 200 carrying "the vendor is busy". Same wait, same
                # backoff - the alternative is telling the user to go and
                # change models over a queue that clears in seconds.
                if attempt >= MAX_RATE_LIMIT_RETRIES:
                    raise
                sleep_for = retry_delay_for_attempt(attempt)
                _on_rate_limit(attempt, sleep_for)
                _sleep(sleep_for)
                continue
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
                        reasoning_effort: Optional[str] = None,
                        thinking: Optional[bool] = DEFAULT_THINKING) -> AdvisorResponse:
    user_content = (
        "Here is the current Elliott Wave engine state as JSON. Give your second opinion.\n\n"
        + json.dumps(context, indent=2, default=str)
    )
    payload = apply_model_options({
        "messages": build_messages(ADVISOR_SYSTEM_PROMPT, user_content),
        "temperature": COMMENTARY_TEMPERATURE,
        "max_tokens": COMMENTARY_MAX_OUTPUT_TOKENS,
    }, seed=seed, reasoning_effort=reasoning_effort, thinking=thinking)
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
                        reasoning_effort: Optional[str] = None,
                        thinking: Optional[bool] = DEFAULT_THINKING) -> WaveCountProposal:
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
        "temperature": ANALYSIS_TEMPERATURE,   # a count is an analysis, not a creative task
        "max_tokens": COUNT_MAX_OUTPUT_TOKENS,
        "response_format": {"type": "json_object"},
    }, seed=seed, reasoning_effort=reasoning_effort, thinking=thinking)
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


def check_access(api_key: str, base_url: Optional[str] = None,
                 client: Optional[httpx.Client] = None, timeout: float = 30.0,
                 model: str = "") -> Dict[str, Any]:
    """Verify the key and the endpoint WITHOUT invoking a model.

    This is the check that ends the guessing. `GET {base}/models` is an
    ordinary catalogue listing: it needs the key, it hits the same host and
    the same base path as every chat call, and it runs no inference at all.
    So its result splits the problem cleanly in two:

      it answers fast  -> the key and the URL are correct, and any slowness
                          afterwards belongs to the model or to the queue in
                          front of it. Nothing on this side to fix.
      it hangs or 4xxs -> the problem is the key, the endpoint or the path,
                          and no amount of waiting on a chat call will
                          diagnose that.

    Before this existed, both cases looked like "the model did not answer",
    which pointed at the slowest possible explanation for what might be a
    one-character typo in a URL."""
    if not api_key:
        raise AIAdvisorError(f"No {PROVIDER_NAME} API key provided "
                             "(get one at https://build.nvidia.com)")

    base = (base_url or DEFAULT_API_BASE).strip().rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    url = f"{base}/models"

    http_client = client or httpx.Client(timeout=build_timeout(timeout))
    owns_client = client is None
    started = time.monotonic()
    try:
        resp = http_client.get(url, headers={"Authorization": f"Bearer {api_key}",
                                             "Accept": "application/json"})
    except httpx.RequestError as exc:
        raise AIAdvisorError(_transport_error_message(exc, url, timeout))
    finally:
        if owns_client:
            http_client.close()

    elapsed = round(time.monotonic() - started, 2)
    if resp.status_code != 200:
        raise AIAdvisorError(
            f"{PROVIDER_NAME} refused the catalogue listing at {url} "
            f"({resp.status_code}): {resp.text[:300]} | This runs no model at all, so it is the key, "
            "the API base URL, or the path - not model speed."
        )

    ids: List[str] = []
    tool_capable: List[str] = []
    model_supports_tools: Optional[bool] = None
    try:
        payload = resp.json()
        entries = payload.get("data") if isinstance(payload, dict) else None
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            name = entry.get("id")
            if not name:
                continue
            ids.append(str(name))
            # OpenRouter publishes `supported_parameters` per model. It is
            # the only authoritative answer to "will the analyst work with
            # this one", and it beats guessing from the model's name.
            supported = entry.get("supported_parameters") or []
            if isinstance(supported, (list, tuple)) and "tools" in supported:
                tool_capable.append(str(name))
    except (ValueError, AttributeError):
        pass

    if model and ids:
        model_supports_tools = model in tool_capable if tool_capable else None

    return {"ok": True, "url": url, "seconds": elapsed, "model_count": len(ids),
            "models": ids, "tool_capable": tool_capable,
            "tool_capable_count": len(tool_capable),
            "model_supports_tools": model_supports_tools,
            "checked_model": model or None}


def ping(api_key: str, model: str = DEFAULT_MODEL, base_url: Optional[str] = None,
         client: Optional[httpx.Client] = None,
         timeout: float = DEFAULT_READ_TIMEOUT,
         thinking: Optional[bool] = DEFAULT_THINKING) -> Dict[str, Any]:
    """One round trip, to separate "the setup is wrong" from "this model is
    slow". Those look identical from the dashboard - a wrong key, a wrong
    URL, a retired model id and a model that thinks for four minutes all
    present as "nothing happened, then an error".

    It STREAMS, and returns the moment the first byte of the answer arrives
    instead of waiting for the whole thing. That distinction is the entire
    point here. A non-streaming check against a reasoning model waits out
    the model's whole thinking phase before it can say anything, so the one
    call meant to diagnose slowness was itself the call most likely to time
    out - which is exactly what happened in production: a 60s timeout on a
    one-token request whose connection was fine.

    What comes back is a diagnosis rather than a yes/no: the time to the
    first token is the number that decides whether an agent run is feasible
    at all, since the analyst pays it once per step."""
    payload = apply_model_options({
        # No max_tokens cap: on most APIs a reasoning model's thinking
        # counts against it, so a small cap can end the generation before
        # any visible token exists - indistinguishable from a hang.
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        "temperature": 0,
    }, seed=None, thinking=thinking)
    if not api_key:
        raise AIAdvisorError(f"No {PROVIDER_NAME} API key provided "
                             "(get one at https://build.nvidia.com)")

    url = chat_url(base_url)
    body = {**payload, "model": model, "stream": True}
    http_client = client or httpx.Client(timeout=build_timeout(timeout))
    owns_client = client is None
    started = time.monotonic()
    try:
        with http_client.stream("POST", url, headers=_headers(api_key, True), json=body) as resp:
            if resp.status_code != 200:
                resp.read()
                raise AIAdvisorError(_api_error_message(resp, model))
            for raw in resp.iter_lines():
                line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(chunk, dict) and chunk.get("error"):
                    error = chunk["error"]
                    message = error.get("message") if isinstance(error, dict) else str(error)
                    raise AIAdvisorError(f"{PROVIDER_NAME} returned an error for '{model}': {message}")
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                text = delta.get("content") or ""
                thinking = delta.get("reasoning_content") or delta.get("reasoning") or ""
                if not text and not thinking:
                    continue
                elapsed = time.monotonic() - started
                # Stop here. The question was "does this setup produce
                # tokens", and it just did; waiting for the rest only risks
                # the timeout this check exists to diagnose.
                return {
                    "ok": True,
                    "model": chunk.get("model") or model,
                    "endpoint": url,
                    "answer": (text or thinking)[:200],
                    "reasoning_first": bool(thinking and not text),
                    "seconds_to_first_token": round(elapsed, 1),
                    "advice": _speed_advice(elapsed),
                }
    except httpx.RequestError as exc:
        raise AIAdvisorError(_transport_error_message(exc, url, timeout))
    finally:
        if owns_client:
            http_client.close()

    raise AIAdvisorError(
        f"{PROVIDER_NAME} accepted the request for '{model}' and closed the stream without sending "
        "a single token. The key, the URL and the model id are therefore fine - this model produced "
        "nothing. Try another model id."
    )


def _speed_advice(seconds: float) -> str:
    """Turn the measurement into the decision it informs. The analyst pays
    the time-to-first-token once per step, so this number decides whether a
    multi-step run is feasible at all."""
    if seconds < 5:
        return "Fast enough for a full analyst run at any step budget."
    if seconds < 20:
        return (f"About {seconds:.0f}s before this model starts answering, so a 10-step analyst run "
                "is several minutes. Workable, but keep the step budget modest.")
    return (f"{seconds:.0f}s before this model even starts answering. The analyst pays that once per "
            "step, so a 10-step run would take far longer than the request budget allows. Use a "
            "faster model for the analyst, lower the reasoning effort, or cut the step budget to 4-6.")


# Headers worth reporting back. An allowlist rather than "everything minus
# secrets", because a diagnostic that echoes arbitrary response headers is
# one upstream change away from leaking something.
_DIAGNOSTIC_HEADERS = ("content-type", "transfer-encoding", "content-length", "retry-after",
                       "x-request-id", "nvcf-reqid", "nvcf-status", "server", "cache-control")


def diagnose(api_key: str, model: str = DEFAULT_MODEL, base_url: Optional[str] = None,
             client: Optional[httpx.Client] = None, timeout: float = 45.0) -> Dict[str, Any]:
    """Report what the chat endpoint ACTUALLY does, rather than what it was
    supposed to do.

    Every other error path here turns a failure into a sentence. That is
    right for users and useless for debugging, because the sentence is
    written from an assumption about the cause. This returns observations:
    the status line, the response headers, whether the body is really
    server-sent events, the first bytes as they arrived, and how long each
    phase took. It never raises on a timeout - a timeout IS the observation,
    and the partial result is the evidence.

    Two things it has caught by design:
      * a `stream: true` request answered with ordinary JSON (some gateways
        ignore the flag), which the SSE reader would wait out in full and
        then report as an empty stream;
      * a 202 with a queue id, which is a "come back later" rather than an
        answer, and looks identical to a hang from the outside."""
    url = chat_url(base_url)
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        "temperature": 0,
        "stream": True,
    }
    report: Dict[str, Any] = {"url": url, "model": model, "sent_bytes": len(json.dumps(body))}
    if not api_key:
        report["error"] = "No API key (neither the request nor the server provided one)."
        return report

    http_client = client or httpx.Client(timeout=build_timeout(timeout))
    owns_client = client is None
    started = time.monotonic()
    try:
        with http_client.stream("POST", url, headers=_headers(api_key, True), json=body) as resp:
            report["status"] = resp.status_code
            report["seconds_to_headers"] = round(time.monotonic() - started, 2)
            report["headers"] = {k: v for k, v in resp.headers.items()
                                 if k.lower() in _DIAGNOSTIC_HEADERS}
            content_type = resp.headers.get("content-type", "")
            report["looks_like_sse"] = "event-stream" in content_type.lower()

            chunks: List[str] = []
            received = 0
            for raw in resp.iter_bytes():
                if not raw:
                    continue
                if "seconds_to_first_byte" not in report:
                    report["seconds_to_first_byte"] = round(time.monotonic() - started, 2)
                received += len(raw)
                if len("".join(chunks)) < 600:
                    chunks.append(raw.decode("utf-8", errors="replace"))
                # Enough to characterise the response. Reading it all would
                # reintroduce the very wait this is meant to measure.
                if received > 2000 or "seconds_to_first_byte" in report and received > 0 and len(chunks) >= 3:
                    break
            report["bytes_received"] = received
            report["first_bytes"] = "".join(chunks)[:600]
    except httpx.RequestError as exc:
        report["error"] = _transport_error_message(exc, url, timeout)
        report["failed_after_seconds"] = round(time.monotonic() - started, 2)
        report.setdefault("phase", "headers" if "status" not in report else "body")
    finally:
        if owns_client:
            http_client.close()

    report["verdict"] = _diagnostic_verdict(report)
    return report


def _diagnostic_verdict(report: Dict[str, Any]) -> str:
    """Say what the observations mean, separately from the observations
    themselves - so a wrong reading here does not hide the raw evidence."""
    if "status" not in report:
        return ("The request never got a response header. That is the connection or the endpoint, "
                "not the model - a model that is merely slow still sends headers immediately.")
    status = report["status"]
    if status == 202:
        return ("202 Accepted: this endpoint queued the request instead of answering it, and expects "
                "the client to poll for the result. A plain chat client waits forever on that, which "
                "is indistinguishable from a slow model.")
    if status != 200:
        return (f"HTTP {status} - the API rejected the request outright. The body above is its "
                "reason; this is a key, model-id or URL problem, not a speed problem.")
    if report.get("bytes_received", 0) == 0:
        return ("Headers came back in "
                f"{report.get('seconds_to_headers', '?')}s but no body bytes followed. The request "
                "was accepted and is sitting in a queue or generating silently - the wait is on the "
                "provider's side, not in this app.")
    if not report.get("looks_like_sse"):
        return ("The response is NOT server-sent events despite stream: true, so the provider "
                "buffered the whole answer. That makes every call wait for the complete generation "
                "- which is exactly the slowness being investigated.")
    return (f"Healthy: headers in {report.get('seconds_to_headers', '?')}s, first body bytes in "
            f"{report.get('seconds_to_first_byte', '?')}s, and the body is a real event stream.")


# The configurations worth telling apart, in the order that isolates the
# variable. Each differs from the one before it by exactly one thing, so
# whichever is the first to answer names the cause outright.
PROBE_VARIANTS = (
    ("thinking off, streamed", {"thinking": False, "stream": True}),
    ("thinking off, not streamed", {"thinking": False, "stream": False}),
    ("thinking on, streamed", {"thinking": True, "stream": True}),
    ("no reasoning field", {"thinking": None, "stream": True}),
)


def probe_variants(api_key: str, model: str = DEFAULT_MODEL, base_url: Optional[str] = None,
                   client: Optional[httpx.Client] = None,
                   per_variant_timeout: float = 25.0) -> Dict[str, Any]:
    """Send the same trivial prompt under several configurations and report
    which ones answer, and how fast.

    This exists because the endpoint diagnostic answered the wrong half of
    the question. It proved the catalogue listing returns in 0.08s while
    the chat endpoint sends no response headers at all for 45s - conclusive
    that the key, URL and network are fine, and that the gateway buffers
    the entire response before sending any of it, so the wait is the full
    generation. What it could not say is WHICH request setting makes that
    generation long.

    So each variant here differs from the previous one by exactly one
    thing, and the first that answers names the cause rather than hinting
    at it: if "thinking off" answers and "thinking on" does not, the
    model's thinking mode is the whole problem, and the fix is a toggle
    rather than a guess about rate limits or model speed."""
    results: List[Dict[str, Any]] = []
    http_client = client or httpx.Client(timeout=build_timeout(per_variant_timeout))
    owns_client = client is None
    url = chat_url(base_url)
    try:
        for label, options in PROBE_VARIANTS:
            body = apply_model_options({
                "model": model,
                "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
                "temperature": 0,
                "max_tokens": 32,
            }, seed=None, thinking=options["thinking"])
            body["stream"] = options["stream"]
            entry: Dict[str, Any] = {"variant": label, "stream": options["stream"],
                                     "thinking": options["thinking"]}
            started = time.monotonic()
            try:
                if options["stream"]:
                    with http_client.stream("POST", url, headers=_headers(api_key, True),
                                            json=body) as resp:
                        entry["status"] = resp.status_code
                        entry["seconds_to_headers"] = round(time.monotonic() - started, 2)
                        for raw in resp.iter_bytes():
                            if raw:
                                entry["seconds_to_first_byte"] = round(time.monotonic() - started, 2)
                                break
                else:
                    resp = http_client.post(url, headers=_headers(api_key, False), json=body)
                    entry["status"] = resp.status_code
                    entry["seconds_to_headers"] = round(time.monotonic() - started, 2)
                    entry["seconds_to_first_byte"] = entry["seconds_to_headers"]
                entry["ok"] = entry.get("status") == 200 and "seconds_to_first_byte" in entry
            except httpx.RequestError as exc:
                entry["ok"] = False
                entry["error"] = _transport_error_message(exc, url, per_variant_timeout)
                entry["gave_up_after"] = round(time.monotonic() - started, 2)
            results.append(entry)
    finally:
        if owns_client:
            http_client.close()

    return {"variants": results, "verdict": _variant_verdict(results)}


def _variant_verdict(results: List[Dict[str, Any]]) -> str:
    working = [r for r in results if r.get("ok")]
    if not working:
        return ("No configuration answered. Since the catalogue listing works, the key and endpoint "
                "are fine - this model is not serving requests right now. Try another model id.")

    fastest = min(working, key=lambda r: r.get("seconds_to_first_byte", 1e9))
    thinking_on = next((r for r in results if r.get("thinking") is True), None)
    thinking_off = next((r for r in results if r.get("thinking") is False and r.get("stream")), None)

    lines = [f"Fastest working setup: {fastest['variant']} "
             f"({fastest.get('seconds_to_first_byte')}s to first byte)."]
    if thinking_off and thinking_on and thinking_off.get("ok") and not thinking_on.get("ok"):
        lines.append("Thinking mode is the cause: with it off the model answers, with it on the "
                     "request never returns. Leave 'Model thinking' off in the AI tab.")
    elif thinking_on and thinking_off and thinking_on.get("ok") and thinking_off.get("ok"):
        lines.append(f"Both thinking modes work "
                     f"(on: {thinking_on.get('seconds_to_first_byte')}s, "
                     f"off: {thinking_off.get('seconds_to_first_byte')}s).")
    streamed = next((r for r in results if r.get("stream") and r.get("ok")), None)
    buffered = next((r for r in results if not r.get("stream") and r.get("ok")), None)
    if buffered and not streamed:
        lines.append("Only the non-streamed request works - this gateway does not serve SSE for "
                     "this model.")
    return " ".join(lines)
