"""Four timeframes, one opinion, and nothing recomputed twice.

The two things worth guarding here are money and honesty. Money: a saved
analysis must be reused when the data has not moved, and must NOT be reused
when it has - a stale count surviving the candle that invalidated it is
worse than paying for a fresh one. Honesty: the percentages next to targets
must be measured from the chart, never asked of a model, because a
percentage reads as measurement even when it is invention.
"""

import json
from types import SimpleNamespace

import pytest

from t3_engine.ai_advisor import analysis_store
from t3_engine.ai_advisor.multi_timeframe import (
    MTF_TIMEFRAMES,
    TimeframeAnalysis,
    run_multi_timeframe,
    synthesise,
)
from t3_engine.ai_advisor.target_odds import (
    MIN_SWINGS_FOR_ODDS,
    annotate_projection,
    odds_for_ratios,
)
from t3_engine.backtest.synthetic_data import generate_synthetic_series
from t3_engine.common.types import Timeframe

CANDLES = generate_synthetic_series(num_cycles=12)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A private database per test - the cache is global state, and tests
    that share it would pass or fail depending on their order."""
    url = f"sqlite:///{tmp_path / 'cache.db'}"
    monkeypatch.setattr(analysis_store, "DEFAULT_DATABASE_URL", url)
    monkeypatch.setattr(analysis_store, "_factory", None)
    return url


# ---- measured odds ----

def test_target_odds_are_measured_from_this_charts_own_swings():
    odds = odds_for_ratios(CANDLES, [0.618, 1.0, 1.618])
    assert odds["sample_size"] >= MIN_SWINGS_FOR_ODDS
    probabilities = [row["probability"] for row in odds["rows"]]
    # A further target cannot be more likely than a nearer one.
    assert probabilities == sorted(probabilities, reverse=True)
    assert all(0.0 <= p <= 1.0 for p in probabilities)
    assert "Not a forecast" in odds["basis"]


def test_too_few_swings_yields_no_percentage_at_all():
    """A number from nine swings is believed exactly as much as one from
    nine hundred, which is the whole problem."""
    assert odds_for_ratios(CANDLES[:40], [1.0]) is None

    annotated = annotate_projection({"targets": [{"ratio": 1.0, "price": 5.0}]}, CANDLES[:40])
    assert "odds" not in annotated
    assert "too few for a base rate" in annotated["odds_note"]


def test_each_target_carries_its_own_measured_probability_and_sample_size():
    projection = {"targets": [{"ratio": 0.618, "price": 5.9}, {"ratio": 1.618, "price": 6.3}]}
    annotated = annotate_projection(projection, CANDLES)
    assert all(t["probability"] is not None for t in annotated["targets"])
    assert annotated["odds"]["sample_size"] >= MIN_SWINGS_FOR_ODDS
    assert annotated["targets"][0]["probability"] >= annotated["targets"][1]["probability"]


# ---- the cache ----

def test_a_saved_analysis_is_reused_while_no_new_candle_has_arrived(store):
    analysis_store.save("bybit", "INJUSDT", "4h", last_candle_time=1000, candle_count=500,
                        payload={"accepted": [{"structure": "IMPULSE"}]}, database_url=store)
    cached = analysis_store.load("bybit", "INJUSDT", "4h", database_url=store)

    assert cached is not None
    assert cached.is_fresh_for(1000)          # same newest candle
    assert cached.is_fresh_for(900)           # nothing newer exists
    assert not cached.is_fresh_for(1001)      # one new candle ends it


def test_freshness_is_exact_because_one_candle_can_end_a_wave(store):
    """A tolerance here is how a stale count survives the bar that
    invalidated it."""
    analysis_store.save("bybit", "X", "1h", last_candle_time=10_000, candle_count=100,
                        payload={}, database_url=store)
    cached = analysis_store.load("bybit", "X", "1h", database_url=store)
    assert not cached.is_fresh_for(10_001)


def test_saving_replaces_rather_than_accumulates(store):
    for newest in (100, 200, 300):
        analysis_store.save("bybit", "X", "5m", newest, 10, {"n": newest}, database_url=store)
    cached = analysis_store.load("bybit", "X", "5m", database_url=store)
    assert cached.payload == {"n": 300}       # a superseded count is never readable


def test_a_corrupt_entry_reads_as_no_entry(store, monkeypatch):
    """Rendering a corrupt payload as a real analysis is worse than
    recomputing."""
    analysis_store.save("bybit", "X", "5m", 100, 10, {"ok": True}, database_url=store)
    from sqlalchemy import update

    from t3_engine.database.models import AnalysisCacheRow
    factory = analysis_store._sessions(store)
    with analysis_store.session_scope(factory) as session:
        session.execute(update(AnalysisCacheRow).values(payload="{not json"))
    assert analysis_store.load("bybit", "X", "5m", database_url=store) is None


def test_clear_is_the_escape_hatch(store):
    analysis_store.save("bybit", "X", "5m", 1, 1, {}, database_url=store)
    analysis_store.save("bybit", "X", "1h", 1, 1, {}, database_url=store)
    assert analysis_store.clear("bybit", "X", database_url=store) == 2
    assert analysis_store.load("bybit", "X", "5m", database_url=store) is None


# ---- orchestration ----

def fake_loader(source, symbol, timeframe, limit, cycles):
    return CANDLES, symbol or "SYN", Timeframe(timeframe)


def make_result(accepted=True):
    return SimpleNamespace(
        accepted=[{"structure": "IMPULSE", "waves": [
            {"label": "1", "start_price": 1.0, "end_price": 2.0, "end_time": 10},
            {"label": "2", "start_price": 2.0, "end_price": 1.5, "end_time": 20}]}] if accepted else [],
        rejected=[], projection={"next_label": "3", "targets": [{"ratio": 1.618, "price": 3.0}]},
        coverage={"covered_fraction": 0.9, "gaps": []}, summary="s", reasoning="r",
        steps_used=4, error="", finished=True)


def test_every_timeframe_is_analysed_and_the_verdict_is_one_extra_call(store, monkeypatch):
    runs, syntheses = [], []
    monkeypatch.setattr("t3_engine.ai_advisor.multi_timeframe.run_analyst",
                        lambda *a, **k: (runs.append(a[2].value), make_result())[1])
    monkeypatch.setattr("t3_engine.ai_advisor.multi_timeframe.synthesise",
                        lambda *a, **k: (syntheses.append(1), {"trend": "UP"})[1])

    result = run_multi_timeframe("sk-or-test", fake_loader, "bybit", "INJUSDT",
                                 database_url=store)

    assert runs == [tf.value for tf in MTF_TIMEFRAMES]
    assert len(syntheses) == 1                 # one reconciliation, not four
    assert result.verdict["trend"] == "UP"
    assert result.recomputed_timeframes == [tf.value for tf in MTF_TIMEFRAMES]


def test_a_second_run_over_unchanged_data_recomputes_nothing(store, monkeypatch):
    """The whole point: a full analyst run is a dozen model calls, and a 4h
    chart produces one new candle every four hours."""
    runs = []
    monkeypatch.setattr("t3_engine.ai_advisor.multi_timeframe.run_analyst",
                        lambda *a, **k: (runs.append(1), make_result())[1])
    monkeypatch.setattr("t3_engine.ai_advisor.multi_timeframe.synthesise",
                        lambda *a, **k: {"trend": "UP"})

    run_multi_timeframe("sk-or-test", fake_loader, "bybit", "INJUSDT", database_url=store)
    first_pass = len(runs)
    second = run_multi_timeframe("sk-or-test", fake_loader, "bybit", "INJUSDT", database_url=store)

    assert len(runs) == first_pass             # not one extra analyst call
    assert second.reused_timeframes == [tf.value for tf in MTF_TIMEFRAMES]
    assert second.recomputed_timeframes == []
    assert "Reused the saved analysis" in second.note
    assert second.per_timeframe[0].accepted    # and the answer still came back


def test_only_the_timeframe_whose_data_moved_is_recomputed(store, monkeypatch):
    runs = []
    monkeypatch.setattr("t3_engine.ai_advisor.multi_timeframe.run_analyst",
                        lambda *a, **k: (runs.append(a[2].value), make_result())[1])
    monkeypatch.setattr("t3_engine.ai_advisor.multi_timeframe.synthesise",
                        lambda *a, **k: {"trend": "UP"})

    run_multi_timeframe("sk-or-test", fake_loader, "bybit", "INJUSDT", database_url=store)
    runs.clear()

    # A new candle arrives on 5m only.
    def moved_loader(source, symbol, timeframe, limit, cycles):
        candles = CANDLES + [CANDLES[-1]] if timeframe == "5m" else CANDLES
        if timeframe == "5m":
            newer = type(CANDLES[-1])(**{**CANDLES[-1].__dict__,
                                         "open_time": CANDLES[-1].open_time + 300_000})
            candles = CANDLES + [newer]
        return candles, symbol, Timeframe(timeframe)

    second = run_multi_timeframe("sk-or-test", moved_loader, "bybit", "INJUSDT", database_url=store)
    assert runs == ["5m"]
    assert second.recomputed_timeframes == ["5m"]
    assert set(second.reused_timeframes) == {"15m", "1h", "4h"}


def test_force_recomputes_everything_regardless_of_the_cache(store, monkeypatch):
    runs = []
    monkeypatch.setattr("t3_engine.ai_advisor.multi_timeframe.run_analyst",
                        lambda *a, **k: (runs.append(1), make_result())[1])
    monkeypatch.setattr("t3_engine.ai_advisor.multi_timeframe.synthesise",
                        lambda *a, **k: {"trend": "UP"})

    run_multi_timeframe("sk-or-test", fake_loader, "bybit", "X", database_url=store)
    runs.clear()
    run_multi_timeframe("sk-or-test", fake_loader, "bybit", "X", database_url=store, force=True)
    assert len(runs) == len(MTF_TIMEFRAMES)


def test_an_empty_run_is_not_cached(store, monkeypatch):
    """Caching "found nothing" would suppress the retry that might have
    worked."""
    monkeypatch.setattr("t3_engine.ai_advisor.multi_timeframe.run_analyst",
                        lambda *a, **k: make_result(accepted=False))
    monkeypatch.setattr("t3_engine.ai_advisor.multi_timeframe.synthesise",
                        lambda *a, **k: {"trend": "MIXED"})

    run_multi_timeframe("sk-or-test", fake_loader, "bybit", "X", database_url=store)
    assert analysis_store.load("bybit", "X", "5m", database_url=store) is None


def test_one_failing_timeframe_does_not_sink_the_others(store, monkeypatch):
    from t3_engine.ai_advisor.advisor import AIAdvisorError

    def flaky(api_key, candles, degree, **kwargs):
        if degree == Timeframe.H1:
            raise AIAdvisorError("rate limited")
        return make_result()

    monkeypatch.setattr("t3_engine.ai_advisor.multi_timeframe.run_analyst", flaky)
    monkeypatch.setattr("t3_engine.ai_advisor.multi_timeframe.synthesise",
                        lambda *a, **k: {"trend": "UP"})

    result = run_multi_timeframe("sk-or-test", fake_loader, "bybit", "X", database_url=store)
    failed = [a for a in result.per_timeframe if a.error]
    assert [a.timeframe for a in failed] == ["1h"]
    assert len([a for a in result.per_timeframe if a.accepted]) == 3


# ---- the synthesis call ----

def test_the_synthesis_sees_conclusions_not_candles(store):
    """The per-timeframe work already happened; asking the model to
    re-derive it would be paying twice in the same request."""
    import httpx

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, text='data: ' + json.dumps({"choices": [
            {"index": 0, "delta": {"content": '{"trend":"UP","headline":"h"}'},
             "finish_reason": "stop"}]}) + "\n\ndata: [DONE]\n\n")

    analyses = [TimeframeAnalysis(timeframe="4h", reused=False, candles=500, last_candle_time=1,
                                  accepted=[{"structure": "IMPULSE", "waves": [
                                      {"label": "1", "start_price": 1, "end_price": 2}]}],
                                  coverage={"covered_fraction": 0.8}, summary="four-hour read")]
    verdict = synthesise("sk-or-test", analyses, "INJUSDT",
                         client=httpx.Client(transport=httpx.MockTransport(handler)))

    assert verdict["trend"] == "UP"
    body = json.dumps(seen)
    assert "four-hour read" in body            # the conclusion travels
    assert "candles" not in seen.get("messages", [{}])[-1].get("content", "")
    system_prompt = seen["messages"][0]["content"]
    assert "MEASURED base rates" in system_prompt
    assert "Do not invent" in system_prompt


def test_no_validated_count_anywhere_is_said_plainly_not_dressed_up():
    verdict = synthesise("sk-or-test", [TimeframeAnalysis(timeframe="5m", reused=False,
                                                          candles=10, last_candle_time=1)],
                         "X")
    assert verdict["conviction"] == "low"
    assert "nothing to reconcile" in verdict["headline"]
