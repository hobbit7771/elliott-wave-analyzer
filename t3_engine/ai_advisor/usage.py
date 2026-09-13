"""What a run actually cost, in tokens and in money.

"Can this be left running around the clock, and what would that cost" is
not a question to answer with a guess. It needs two real numbers: how many
tokens one run of this agent consumes, and what this provider charges for
them. Both are obtainable:

  - The token counts come back from the provider itself, in the usage
    trailer of each streamed response (see advisor.py's `_consume_stream`,
    which used to drop it). `UsageMeter` adds them up across the dozen or
    so calls one analyst run makes.
  - The prices come from the provider's own catalogue - OpenRouter's
    `/v1/models` publishes a `pricing` object per model, in dollars per
    token. They are READ, never hardcoded: a price typed into this
    repository would be wrong the first time the provider changed it, and
    wrong silently.

Everything here is measurement. Nothing extrapolates a monthly bill from a
single run without being asked to, and `monthly_estimate` states the
assumption it runs on (how many analyses per hour) rather than burying it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx


@dataclass
class UsageMeter:
    """Token counts summed over every call in one run."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    # Tokens the provider served from its prompt cache. Worth separating:
    # they are usually charged at a fraction of the normal input price, and
    # an agent loop that resends its conversation every step is exactly the
    # shape that hits a cache.
    cached_tokens: int = 0
    per_call: List[Dict[str, int]] = field(default_factory=list)

    def add(self, usage: Optional[Dict[str, Any]]) -> None:
        if not isinstance(usage, dict):
            return
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        details = usage.get("completion_tokens_details") or {}
        reasoning = int(details.get("reasoning_tokens") or 0) if isinstance(details, dict) else 0
        prompt_details = usage.get("prompt_tokens_details") or {}
        cached = int(prompt_details.get("cached_tokens") or 0) if isinstance(prompt_details, dict) else 0

        self.calls += 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.reasoning_tokens += reasoning
        self.cached_tokens += cached
        self.per_call.append({"prompt_tokens": prompt, "completion_tokens": completion})

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> Dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens,
        }


def model_pricing(models_payload: Any, model_id: str) -> Optional[Dict[str, float]]:
    """This model's published price per token, from the provider catalogue.

    Returns None rather than a default when the model is not listed or
    carries no prices: a made-up price is worse than an admitted gap,
    because it produces a confident cost estimate with nothing behind it."""
    rows = []
    if isinstance(models_payload, dict):
        rows = models_payload.get("data") or []
    elif isinstance(models_payload, list):
        rows = models_payload
    for row in rows:
        if not isinstance(row, dict) or row.get("id") != model_id:
            continue
        pricing = row.get("pricing")
        if not isinstance(pricing, dict):
            return None
        out: Dict[str, float] = {}
        for key in ("prompt", "completion", "input_cache_read"):
            try:
                value = float(pricing[key])
            except (KeyError, TypeError, ValueError):
                continue
            out[key] = value
        return out or None
    return None


def cost_of(meter: UsageMeter, pricing: Optional[Dict[str, float]]) -> Optional[float]:
    """Dollars for one run, or None when the price is not known.

    Cached input tokens are billed at the cache-read rate where the
    provider publishes one, and the uncached remainder at the normal input
    rate."""
    if not pricing or "prompt" not in pricing or "completion" not in pricing:
        return None
    cached = min(meter.cached_tokens, meter.prompt_tokens)
    fresh = meter.prompt_tokens - cached
    cache_rate = pricing.get("input_cache_read", pricing["prompt"])
    return (fresh * pricing["prompt"]
            + cached * cache_rate
            + meter.completion_tokens * pricing["completion"])


def monthly_estimate(cost_per_run: Optional[float], runs_per_hour: float) -> Optional[Dict[str, float]]:
    """What that run rate costs over a day and a 30-day month.

    Straight multiplication, and the assumption is the caller's to state:
    this function invents no run rate of its own."""
    if cost_per_run is None or runs_per_hour <= 0:
        return None
    hourly = cost_per_run * runs_per_hour
    return {
        "runs_per_hour": runs_per_hour,
        "per_run": round(cost_per_run, 6),
        "per_hour": round(hourly, 6),
        "per_day": round(hourly * 24, 4),
        "per_month": round(hourly * 24 * 30, 2),
    }


def fetch_pricing(api_key: str, model_id: str, base_url: str,
                  client: Optional[httpx.Client] = None,
                  timeout: float = 20.0) -> Optional[Dict[str, float]]:
    """Read this model's price from the provider's catalogue. Never raises:
    a cost figure is useful, and failing to get one must not fail the run
    it was measuring."""
    http_client = client or httpx.Client(timeout=timeout)
    try:
        url = base_url.rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        resp = http_client.get(url, headers=headers)
        if resp.status_code != 200:
            return None
        return model_pricing(resp.json(), model_id)
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        if client is None:
            http_client.close()
