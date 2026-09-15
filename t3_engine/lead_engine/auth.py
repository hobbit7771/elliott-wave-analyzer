"""Read-only access control for the Lead Engine's external interface.

Three rules, and they are the whole module:

  1. The external namespace is OFF unless `EXTERNAL_AI_ACCESS_ENABLED` is
     true. Off means the routes answer 404-shaped JSON and touch no
     engine state; the core Lead Engine keeps running either way, which is
     the point of it being a separate switch from LEAD_ENGINE_ENABLED.

  2. Every external request carries a token. `LEAD_ENGINE_API_KEY` lives
     in the environment and nowhere else - not in the frontend, not in a
     file, not in this repository. Both `Authorization: Bearer <token>`
     and `x-api-key: <token>` are accepted because different clients
     default to different ones and there is no reason to make that the
     caller's problem.

  3. The token grants READ ONLY. There is no write path to grant: this
     package cannot place an order, cannot change leverage, cannot touch
     an exchange key and cannot reach the analyser's paper-trading engine.
     That is a property of the architecture rather than a check here -
     `test_lead_engine_isolation.py` asserts the imports that would be
     needed do not exist - but it is stated here too because a reader of
     an auth module is entitled to know what the token is worth.

Comparison is constant-time. A token check that returns early on the first
wrong byte leaks the token a byte at a time to anyone patient.
"""

from __future__ import annotations

import hmac
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple

API_KEY_ENV = "LEAD_ENGINE_API_KEY"
API_KEY_ENV_PREFIXED = "T3_LEAD_ENGINE_API_KEY"
EXTERNAL_ENV = "EXTERNAL_AI_ACCESS_ENABLED"
EXTERNAL_ENV_PREFIXED = "T3_EXTERNAL_AI_ACCESS_ENABLED"

_TRUE = {"1", "true", "yes", "on"}

# Rate limit. The brief asks for 5-10 requests/second per token and for
# the limit not to get in the way of an analysis once a second - so ten a
# second, measured over a rolling window, is comfortably above the use
# case and well below anything that could hurt the process.
RATE_LIMIT_PER_SECOND = 10
RATE_WINDOW_SECONDS = 1.0

# A burst allowance, so a client fetching eight endpoints in one go to
# build a picture is not punished for it.
BURST = 30
BURST_WINDOW_SECONDS = 10.0


def external_enabled() -> bool:
    for name in (EXTERNAL_ENV, EXTERNAL_ENV_PREFIXED):
        raw = os.getenv(name)
        if raw is not None:
            return raw.strip().lower() in _TRUE
    return False


def configured_key() -> str:
    for name in (API_KEY_ENV, API_KEY_ENV_PREFIXED):
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def token_from_headers(headers) -> str:
    """Bearer or x-api-key, whichever the client sent."""
    authorization = (headers.get("authorization") or headers.get("Authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return (headers.get("x-api-key") or headers.get("X-API-Key") or "").strip()


@dataclass
class AuthResult:
    ok: bool
    status: int = 200
    reason: str = ""
    token_id: str = ""

    def as_error(self) -> Dict[str, object]:
        return {"error": self.reason, "status": self.status, "read_only": True}


class RateLimiter:
    """Per-token sliding window. In-process, which is the right scope: one
    web process, one limiter, no shared state to get out of step."""

    def __init__(self, per_second: int = RATE_LIMIT_PER_SECOND,
                 burst: int = BURST) -> None:
        self.per_second = per_second
        self.burst = burst
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)

    def check(self, token_id: str, now: Optional[float] = None) -> Tuple[bool, str]:
        now = time.time() if now is None else now
        hits = self._hits[token_id]
        while hits and now - hits[0] > BURST_WINDOW_SECONDS:
            hits.popleft()
        recent = sum(1 for stamp in hits if now - stamp <= RATE_WINDOW_SECONDS)
        if recent >= self.per_second:
            return False, (f"rate limit: {self.per_second} requests/second per token "
                           f"({recent} in the last second)")
        if len(hits) >= self.burst:
            return False, (f"rate limit: {self.burst} requests per "
                           f"{BURST_WINDOW_SECONDS:.0f}s per token")
        hits.append(now)
        return True, ""

    def reset(self) -> None:
        self._hits.clear()


_limiter = RateLimiter()


def limiter() -> RateLimiter:
    return _limiter


def authorize(headers, limit: bool = True) -> AuthResult:
    """The one check every external route makes."""
    if not external_enabled():
        return AuthResult(False, 404, "External access is disabled. Set "
                                      f"{EXTERNAL_ENV}=true to enable the read-only API.")
    expected = configured_key()
    if not expected:
        # Refusing rather than opening: an unset key is a deployment that
        # has not decided, and defaulting that to "no auth" is how an
        # open endpoint ships.
        return AuthResult(False, 503, f"{API_KEY_ENV} is not set on this deployment, so "
                                      "the read-only API refuses every request.")
    supplied = token_from_headers(headers)
    if not supplied:
        return AuthResult(False, 401, "Missing token. Send Authorization: Bearer <token> "
                                      "or x-api-key: <token>.")
    if not hmac.compare_digest(supplied, expected):
        # Constant-time, and the message never says which part was wrong.
        return AuthResult(False, 401, "Invalid token.")
    token_id = supplied[:6]
    if limit:
        allowed, reason = _limiter.check(token_id)
        if not allowed:
            return AuthResult(False, 429, reason, token_id)
    return AuthResult(True, 200, "", token_id)
