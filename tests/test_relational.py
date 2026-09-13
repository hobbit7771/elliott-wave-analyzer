"""kumo-relational: the trade-quality predictor.

A different kind of model from the rest of ai_advisor/ - no text output, no
tool calling, so it cannot label waves or replace the chat model. What it
can do is the one job the engine already has the table for: given how a
setup scored, how often did setups like it work out.

These tests care most about the two ways this could quietly mislead - a
probability produced from a history that cannot support one, and lookahead
sneaking in through the context rows.
"""

import json

import httpx
import pytest

from t3_engine.ai_advisor.advisor import AIAdvisorError
from t3_engine.ai_advisor.relational import (
    MIN_CONTEXT_TRADES,
    RelationalUnavailable,
    TradeRow,
    build_payload,
    predict_trade_quality,
    usable_context,
)


def row(index, won=None, closed_ms=None, confidence=80.0):
    return TradeRow(
        signal_id=f"sig-{index}",
        anchor_time="2026-01-01T00:00:00Z",
        anchor_ms=index * 60_000,
        features={"confidence": confidence, "risk_reward": 2.0, "elliott": 0.8,
                  "price_action": 0.7, "fibonacci": 0.6, "volume": 0.5,
                  "momentum": 0.5, "derivatives": 0.4, "orderbook": 0.4, "higher_tf": 0.5},
        categories={"wave_label": "3", "side": "LONG", "entry_stage": "AGGRESSIVE",
                    "timeframe": "5m"},
        won=won,
        closed_ms=closed_ms,
    )


def mixed_history(count=MIN_CONTEXT_TRADES + 4):
    return [row(i, won=(i % 2 == 0), closed_ms=i * 1000) for i in range(count)]


def make_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok_response(n):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"predictions": [
            {"prediction": True, "probabilities": {"true": 0.71, "false": 0.29}} for _ in range(n)]})
    return handler


# ---- refusing to answer when the history cannot support an answer ----

def test_too_few_closed_trades_is_refused_rather_than_answered():
    """A probability computed from four trades would be believed, and
    should not be."""
    with pytest.raises(RelationalUnavailable, match="at least"):
        predict_trade_quality("nvapi-test", mixed_history(4), [row(99)],
                              client=make_client(ok_response(1)))


def test_a_history_where_everything_won_teaches_nothing_and_is_refused():
    """A table with one outcome can only repeat that outcome back."""
    all_wins = [row(i, won=True, closed_ms=i * 1000) for i in range(MIN_CONTEXT_TRADES + 2)]
    with pytest.raises(RelationalUnavailable, match="same outcome"):
        predict_trade_quality("nvapi-test", all_wins, [row(99)],
                              client=make_client(ok_response(1)))


def test_nothing_to_score_is_refused_too():
    with pytest.raises(RelationalUnavailable, match="No signals to score"):
        predict_trade_quality("nvapi-test", mixed_history(), [],
                              client=make_client(ok_response(0)))


def test_no_key_fails_before_anything_is_sent():
    with pytest.raises(AIAdvisorError, match="No NVIDIA API key"):
        predict_trade_quality("", mixed_history(), [row(99)])


# ---- no lookahead ----

def test_context_only_contains_trades_that_had_already_closed():
    """The same guarantee the wave engine is built on. The model is given
    anchor_time and could enforce this itself; relying on that would make
    the guarantee depend on someone else honouring it."""
    history = [row(0, won=True, closed_ms=1_000), row(1, won=False, closed_ms=5_000),
               row(2, won=True, closed_ms=9_000)]
    usable = usable_context(history, before_ms=5_000)
    assert [r.signal_id for r in usable] == ["sig-0", "sig-1"]


def test_trades_that_never_closed_are_not_context():
    """An open trade has no outcome, so it can only add noise labelled as
    fact."""
    history = mixed_history() + [row(999, won=None, closed_ms=None)]
    assert all(r.won is not None for r in usable_context(history))


def test_context_is_ordered_oldest_first_and_capped():
    from t3_engine.ai_advisor.relational import MAX_CONTEXT_TRADES

    history = [row(i, won=(i % 3 == 0), closed_ms=i * 1000) for i in range(MAX_CONTEXT_TRADES + 50)]
    usable = usable_context(history)
    assert len(usable) == MAX_CONTEXT_TRADES
    assert usable[0].closed_ms < usable[-1].closed_ms      # the NEWEST window, in order


# ---- payload shape ----

def test_the_payload_declares_the_task_and_keeps_the_tables_joinable():
    """A schema error in a payload this shape comes back as a 400 with no
    clue which of thirty fields was wrong, so the join keys are asserted
    here instead."""
    payload = build_payload(mixed_history(), [row(99)])

    assert payload["task"]["kind"] == "binary_classification"
    assert payload["task"]["target"]["positive_class"] == "true"
    relationship = payload["schema"]["relationships"][0]
    assert relationship["source_columns"] == ["instance_id", "signal_row_id"]
    assert relationship["target_table"] == "signals"

    context_ids = [r[0] for r in payload["context"]["instance_table"]["rows"]]
    related_ids = [r[0] for r in payload["context"]["related_tables"]["signals"]["rows"]]
    assert context_ids == related_ids      # every instance has its features

    # Predicted rows must not collide with context rows on instance_id.
    predict_ids = [r[0] for r in payload["predict"]["instance_table"]["rows"]]
    assert not set(predict_ids) & set(context_ids)


def test_the_label_column_carries_the_real_outcome():
    payload = build_payload([row(0, won=True, closed_ms=1), row(1, won=False, closed_ms=2)], [row(9)])
    labels = [r[3] for r in payload["context"]["instance_table"]["rows"]]
    assert labels == [True, False]


def test_predicted_rows_carry_no_label_column():
    """A label in the predict table is the answer leaking into the
    question."""
    payload = build_payload(mixed_history(), [row(99)])
    assert "label" not in payload["predict"]["instance_table"]["columns"]


def test_every_feature_column_reaches_the_request():
    from t3_engine.ai_advisor.relational import CATEGORICAL_COLUMNS, FEATURE_COLUMNS

    payload = build_payload(mixed_history(), [row(99)])
    columns = payload["context"]["related_tables"]["signals"]["columns"]
    for name in FEATURE_COLUMNS + CATEGORICAL_COLUMNS:
        assert name in columns
    assert len(payload["context"]["related_tables"]["signals"]["rows"][0]) == len(columns)


# ---- reading the answer back ----

def test_probabilities_are_read_from_a_dict_of_class_names():
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"predictions": [
            {"prediction": True, "probabilities": {"true": 0.82, "false": 0.18}}]})

    result = predict_trade_quality("nvapi-test", mixed_history(), [row(99)],
                                   client=make_client(handler))
    assert result.predictions[0].win_probability == 0.82
    assert result.predictions[0].signal_id == "sig-99"
    assert sent["model"] == "kumo-relational"


def test_probabilities_are_also_read_from_a_list_ordered_by_declared_classes():
    """Providers differ on the shape, and classes are declared
    ["false", "true"] - reading index 0 would invert every answer."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"predictions": [
            {"prediction": False, "probabilities": [0.9, 0.1]}]})

    result = predict_trade_quality("nvapi-test", mixed_history(), [row(99)],
                                   client=make_client(handler))
    assert result.predictions[0].win_probability == 0.1


def test_a_missing_probability_is_none_rather_than_a_guess():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"predictions": [{"something_else": 1}]})

    result = predict_trade_quality("nvapi-test", mixed_history(), [row(99)],
                                   client=make_client(handler))
    assert result.predictions[0].win_probability is None


def test_an_api_error_names_the_right_host():
    """This lives on a different host from the chat models, so a 404 here
    means the relational URL, not the chat one."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    with pytest.raises(AIAdvisorError, match="relational URL, not the chat one"):
        predict_trade_quality("nvapi-test", mixed_history(), [row(99)],
                              client=make_client(handler))


def test_the_result_reports_what_the_context_actually_was():
    """A probability without the size and balance of the history behind it
    invites more trust than it has earned."""
    result = predict_trade_quality("nvapi-test", mixed_history(16), [row(99)],
                                   client=make_client(ok_response(1)))
    assert result.context_trades == 16
    assert 0 < result.wins_in_context < 16


# ---- the bridge from a real engine run ----

def test_rows_from_backtest_separates_known_outcomes_from_open_signals():
    """Context is what the engine already learned; predict is what a
    probability would actually inform."""
    from types import SimpleNamespace

    from t3_engine.ai_advisor.relational import rows_from_backtest

    def signal(sid, decision="SIGNAL_ACCEPTED"):
        return SimpleNamespace(
            signal_id=sid, created_at=1_700_000_000_000, confidence=81.0, risk_reward=2.4,
            score_breakdown={"elliott": 0.9, "price_action": 0.6, "fibonacci": 0.7, "volume": 0.4,
                             "momentum": 0.5, "derivatives": 0.3, "orderbook": 0.2, "higher_tf": 0.6},
            wave_label=SimpleNamespace(value="3"), side=SimpleNamespace(value="LONG"),
            entry_stage=SimpleNamespace(value="AGGRESSIVE"), decision=decision)

    def position(sid, pnl):
        return SimpleNamespace(signal_id=sid, realized_pnl=pnl, closed=True,
                               closed_at=1_700_000_100_000)

    signals = [signal("a"), signal("b"), signal("c"), signal("d", decision="SIGNAL_REJECTED")]
    closed = [position("a", 120.0), position("b", -80.0)]

    context, predict = rows_from_backtest(signals, closed, "1h")

    assert [r.signal_id for r in context] == ["a", "b"]
    assert [r.won for r in context] == [True, False]
    assert [r.signal_id for r in predict] == ["c"]      # open signal only
    assert predict[0].won is None
    # A rejected signal was never traded: no outcome to learn from, and
    # nothing to predict either.
    assert "d" not in [r.signal_id for r in context + predict]
    assert context[0].categories["timeframe"] == "1h"
    assert context[0].features["elliott"] == 0.9
