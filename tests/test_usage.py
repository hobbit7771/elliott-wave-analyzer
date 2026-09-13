"""Token and cost accounting.

"Can this run around the clock and what does that cost" must be answered
with measured numbers, not a guess: token counts come from the provider's
own usage trailer, prices from its own catalogue.
"""

from t3_engine.ai_advisor.usage import (
    UsageMeter,
    cost_of,
    model_pricing,
    monthly_estimate,
)

CATALOGUE = {"data": [
    {"id": "~openai/gpt-astra-latest",
     "pricing": {"prompt": "0.0000025", "completion": "0.00001",
                 "input_cache_read": "0.00000025"}},
    {"id": "free/model", "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "no/prices"},
]}


def test_usage_is_summed_across_every_call_in_a_run():
    """One analyst run is a dozen calls. A per-call number answers nothing."""
    meter = UsageMeter()
    meter.add({"prompt_tokens": 1000, "completion_tokens": 200,
               "completion_tokens_details": {"reasoning_tokens": 150},
               "prompt_tokens_details": {"cached_tokens": 800}})
    meter.add({"prompt_tokens": 1500, "completion_tokens": 300})
    assert meter.calls == 2
    assert meter.prompt_tokens == 2500
    assert meter.completion_tokens == 500
    assert meter.reasoning_tokens == 150
    assert meter.cached_tokens == 800
    assert meter.total_tokens == 3000


def test_a_response_without_usage_is_ignored_not_counted_as_zero():
    meter = UsageMeter()
    meter.add(None)
    meter.add("not a dict")
    assert meter.calls == 0


def test_prices_are_read_from_the_catalogue():
    pricing = model_pricing(CATALOGUE, "~openai/gpt-astra-latest")
    assert pricing["prompt"] == 0.0000025
    assert pricing["completion"] == 0.00001
    assert pricing["input_cache_read"] == 0.00000025


def test_an_unknown_or_unpriced_model_gives_no_price_rather_than_a_default():
    """A made-up price is worse than an admitted gap: it produces a
    confident cost figure with nothing behind it."""
    assert model_pricing(CATALOGUE, "nope/nope") is None
    assert model_pricing(CATALOGUE, "no/prices") is None
    assert cost_of(UsageMeter(prompt_tokens=100), None) is None


def test_cached_input_tokens_are_billed_at_the_cache_rate():
    """An agent loop resends its conversation every step, which is exactly
    the shape that hits a prompt cache - charging all of it at the full
    input rate would overstate the bill several times over."""
    meter = UsageMeter(prompt_tokens=10_000, completion_tokens=1_000, cached_tokens=8_000)
    pricing = model_pricing(CATALOGUE, "~openai/gpt-astra-latest")
    cost = cost_of(meter, pricing)
    # 2000 fresh @ 2.5e-6 + 8000 cached @ 2.5e-7 + 1000 out @ 1e-5
    assert round(cost, 8) == round(2000 * 0.0000025 + 8000 * 0.00000025 + 1000 * 0.00001, 8)


def test_without_a_published_cache_rate_cached_tokens_cost_the_input_rate():
    meter = UsageMeter(prompt_tokens=1000, completion_tokens=100, cached_tokens=500)
    cost = cost_of(meter, {"prompt": 0.000001, "completion": 0.000002})
    assert cost == 1000 * 0.000001 + 100 * 0.000002


def test_a_free_model_costs_zero_and_says_so():
    meter = UsageMeter(prompt_tokens=50_000, completion_tokens=5_000)
    assert cost_of(meter, model_pricing(CATALOGUE, "free/model")) == 0.0


def test_the_monthly_estimate_names_its_run_rate_instead_of_assuming_one():
    """Continuous monitoring is just a run rate. Four analyses an hour is a
    different bill from forty, and the assumption has to be visible."""
    est = monthly_estimate(0.05, runs_per_hour=12)
    assert est["runs_per_hour"] == 12
    assert est["per_hour"] == 0.6
    assert est["per_day"] == 14.4
    assert est["per_month"] == 432.0
    assert monthly_estimate(None, 12) is None
    assert monthly_estimate(0.05, 0) is None
