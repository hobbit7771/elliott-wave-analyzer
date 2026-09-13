"""NVIDIA `kumo-relational`: a trade-quality predictor, not a second analyst.

This model is a different kind of thing from everything else in
ai_advisor/. It is not a chat model: it has no text output and no tool
calling, so it cannot label waves, cannot hold a conversation, and cannot
replace the model behind the AI Analyst. What it does take is a RELATIONAL
SCHEMA plus rows, and what it returns is a prediction and a probability per
row.

That fits exactly one job here, and fits it well: the engine already
produces a labelled table of its own history - every signal it scored, and
for the ones that became trades, whether they ended green. So the question
"given how this setup scored, how often did setups like it work out?" is a
binary classification over the engine's own past, which is precisely this
model's shape.

WHAT THIS IS NOT ALLOWED TO DO. It never gates a trade, never moves a
stop, and never edits a count. The hard Elliott rules and the risk engine
decide; this is a number shown next to a signal, in the same advisory
position as the second opinion. A model trained on a few dozen of its own
past trades is a hint, not an edge, and treating it as one would be the
same mistake as curve-fitting the parameters.

NO LOOKAHEAD. Context rows are filtered to trades that had already CLOSED
before the signal being predicted was created. The model also gets
`anchor_time` so it can enforce that itself, but this module does not rely
on that: the filter is applied here, on data this server owns, for the same
reason external wave counts are re-validated here rather than trusted.

ENDPOINT NOTE: this lives on a different host from the chat models
(`ai.api.nvidia.com`, not `integrate.api.nvidia.com`) though it takes the
same key. Like every other endpoint here it is overridable, and the sandbox
this was written in cannot reach it, so the request shape follows NVIDIA's
published example rather than a verified call.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from t3_engine.ai_advisor.advisor import AIAdvisorError, build_timeout, _transport_error_message

DEFAULT_RELATIONAL_MODEL = "kumo-relational"
DEFAULT_RELATIONAL_URL = os.getenv(
    "T3_RELATIONAL_API_URL",
    "https://ai.api.nvidia.com/v1/structured-data/nvidia/kumo-relational/predictions",
)

# Few-shot in spirit, but a handful of rows cannot separate a winning setup
# from a losing one - and a confident number produced from four trades is
# worse than no number, because it will be believed. Both classes must also
# be present: a table where everything won teaches only "everything wins".
MIN_CONTEXT_TRADES = 12
MAX_CONTEXT_TRADES = 500

# The engine's own score components, which are the whole point: this asks
# whether the weighting the engine already applies actually predicted
# anything, using the parts rather than the total.
FEATURE_COLUMNS = ("confidence", "risk_reward", "elliott", "price_action", "fibonacci",
                   "volume", "momentum", "derivatives", "orderbook", "higher_tf")
CATEGORICAL_COLUMNS = ("wave_label", "side", "entry_stage", "timeframe")


class RelationalUnavailable(Exception):
    """Not enough history to ask the question honestly. Distinct from an API
    failure: nothing is wrong, there is simply nothing to learn from yet."""


@dataclass
class TradeRow:
    """One scored signal, plus its outcome when it has one."""
    signal_id: str
    anchor_time: str            # ISO-8601, UTC
    anchor_ms: int
    features: Dict[str, float]
    categories: Dict[str, str]
    won: Optional[bool] = None
    closed_ms: Optional[int] = None


@dataclass
class RelationalPrediction:
    signal_id: str
    win_probability: Optional[float]
    prediction: Any = None


@dataclass
class RelationalResult:
    predictions: List[RelationalPrediction] = field(default_factory=list)
    context_trades: int = 0
    wins_in_context: int = 0
    model: str = DEFAULT_RELATIONAL_MODEL
    raw: Dict[str, Any] = field(default_factory=dict)


def _column_spec() -> Dict[str, Dict[str, Any]]:
    spec: Dict[str, Dict[str, Any]] = {
        "instance_id": {"dtype": "int64", "stype": "ID", "nullable": False},
        "signal_row_id": {"dtype": "int64", "stype": "ID", "nullable": False},
    }
    for name in FEATURE_COLUMNS:
        spec[name] = {"dtype": "float64", "stype": "numerical"}
    for name in CATEGORICAL_COLUMNS:
        spec[name] = {"dtype": "string", "stype": "categorical"}
    return spec


def build_payload(context: List[TradeRow], predict: List[TradeRow],
                  model: str = DEFAULT_RELATIONAL_MODEL) -> Dict[str, Any]:
    """Assemble the request. Kept separate from the call so the exact bytes
    can be inspected in a test - a schema error in a payload this shape is
    otherwise a 400 with no clue which of thirty fields was wrong."""
    signal_columns = ["instance_id", "signal_row_id"] + list(FEATURE_COLUMNS) + list(CATEGORICAL_COLUMNS)

    def signal_row(index: int, row: TradeRow) -> List[Any]:
        return ([index, index]
                + [float(row.features.get(name, 0.0)) for name in FEATURE_COLUMNS]
                + [str(row.categories.get(name, "")) for name in CATEGORICAL_COLUMNS])

    offset = len(context)
    return {
        "model": model,
        "task": {
            "kind": "binary_classification",
            "target": {
                "column_name": "label",
                "dtype": "bool",
                "classes": ["false", "true"],
                "positive_class": "true",
            },
            "entity_table_names": ["signals"],
            "anchor_time_column": "anchor_time",
        },
        "schema": {
            "instance_table": {
                "columns": {
                    "instance_id": {"dtype": "int64", "stype": "ID", "nullable": False},
                    "anchor_time": {"dtype": "timestamp[us]", "stype": "timestamp", "nullable": False},
                    "signal_row_id": {"dtype": "int64", "stype": "ID", "nullable": False},
                    "label": {"dtype": "bool", "stype": "categorical"},
                },
                "primary_key": "instance_id",
            },
            "related_tables": {
                "signals": {"columns": _column_spec(),
                            "primary_key": ["instance_id", "signal_row_id"]},
            },
            "relationships": [{
                "source_columns": ["instance_id", "signal_row_id"],
                "target_table": "signals",
                "target_columns": ["instance_id", "signal_row_id"],
            }],
        },
        "context": {
            "instance_table": {
                "format": "arrays",
                "columns": ["instance_id", "anchor_time", "signal_row_id", "label"],
                "rows": [[i, row.anchor_time, i, bool(row.won)] for i, row in enumerate(context)],
            },
            "related_tables": {
                "signals": {"format": "arrays", "columns": signal_columns,
                            "rows": [signal_row(i, row) for i, row in enumerate(context)]},
            },
        },
        "predict": {
            "instance_table": {
                "format": "arrays",
                "columns": ["instance_id", "anchor_time", "signal_row_id"],
                "rows": [[offset + i, row.anchor_time, offset + i] for i, row in enumerate(predict)],
            },
            "related_tables": {
                "signals": {"format": "arrays", "columns": signal_columns,
                            "rows": [signal_row(offset + i, row) for i, row in enumerate(predict)]},
            },
        },
        "output": {"fields": ["prediction", "probabilities"]},
    }


def usable_context(context: List[TradeRow], before_ms: Optional[int] = None) -> List[TradeRow]:
    """Trades that had already CLOSED before the moment being predicted.

    Applied here rather than left to the model's own anchor_time handling,
    for the same reason an external wave count is re-validated on this
    server: a no-lookahead guarantee that depends on someone else honouring
    it is not a guarantee."""
    rows = [row for row in context if row.won is not None and row.closed_ms is not None]
    if before_ms is not None:
        rows = [row for row in rows if row.closed_ms <= before_ms]
    rows.sort(key=lambda row: row.closed_ms or 0)
    return rows[-MAX_CONTEXT_TRADES:]


def _win_probability(entry: Dict[str, Any]) -> Optional[float]:
    """Pull the positive-class probability out of whatever shape came back.
    Providers differ on whether probabilities are a dict of class names or a
    list ordered by the declared classes."""
    probabilities = entry.get("probabilities")
    if isinstance(probabilities, dict):
        for key in ("true", "True", "1", 1, True):
            if key in probabilities:
                return float(probabilities[key])
        return None
    if isinstance(probabilities, (list, tuple)) and len(probabilities) == 2:
        return float(probabilities[1])       # classes are declared ["false", "true"]
    prediction = entry.get("prediction")
    if isinstance(prediction, bool):
        return 1.0 if prediction else 0.0
    return None


def predict_trade_quality(api_key: str, context: List[TradeRow], predict: List[TradeRow],
                          model: str = DEFAULT_RELATIONAL_MODEL, url: Optional[str] = None,
                          client: Optional[httpx.Client] = None,
                          timeout: float = 120.0) -> RelationalResult:
    """Ask how often setups scoring like these worked out before.

    Raises RelationalUnavailable when the history cannot answer the
    question - too few closed trades, or all of them the same outcome. That
    is not an error to be smoothed over: a probability computed from four
    trades, or from a table where everything won, would be believed and
    should not be."""
    if not api_key:
        raise AIAdvisorError("No NVIDIA API key provided (get one at https://build.nvidia.com)")
    if not predict:
        raise RelationalUnavailable("No signals to score.")

    usable = usable_context(context)
    if len(usable) < MIN_CONTEXT_TRADES:
        raise RelationalUnavailable(
            f"Only {len(usable)} closed trade(s) in this history; at least {MIN_CONTEXT_TRADES} are "
            "needed before a win probability means anything. Run a longer backtest."
        )
    wins = sum(1 for row in usable if row.won)
    if wins == 0 or wins == len(usable):
        raise RelationalUnavailable(
            f"All {len(usable)} closed trades had the same outcome "
            f"({'wins' if wins else 'losses'}), so there is nothing to tell apart. "
            "A probability from that table would only repeat it back."
        )

    endpoint = (url or DEFAULT_RELATIONAL_URL).strip()
    payload = build_payload(usable, predict, model=model)
    http_client = client or httpx.Client(timeout=build_timeout(timeout))
    owns_client = client is None
    try:
        resp = http_client.post(endpoint, headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }, json=payload)
    except httpx.RequestError as exc:
        raise AIAdvisorError(_transport_error_message(exc, endpoint, timeout))
    finally:
        if owns_client:
            http_client.close()

    if resp.status_code != 200:
        raise AIAdvisorError(
            f"kumo-relational error {resp.status_code}: {resp.text[:400]} | This is a different host "
            "from the chat models, so a 404 here means the relational URL, not the chat one."
        )

    data = resp.json()
    rows = data.get("predictions") or data.get("results") or data.get("data") or []
    predictions = [
        RelationalPrediction(signal_id=predict[i].signal_id,
                             win_probability=_win_probability(entry if isinstance(entry, dict) else {}),
                             prediction=(entry or {}).get("prediction") if isinstance(entry, dict) else entry)
        for i, entry in enumerate(rows[:len(predict)])
    ]
    return RelationalResult(predictions=predictions, context_trades=len(usable),
                            wins_in_context=wins, model=model, raw=data)


def _iso(ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rows_from_backtest(signals, closed_positions, timeframe: str) -> tuple:
    """Turn an engine run into (context, predict).

    Context is every ACCEPTED signal that became a trade and closed, so its
    outcome is known. Predict is every accepted signal still without a
    closed trade - the ones a probability would actually inform. Rejected
    signals are deliberately excluded from both: the engine never traded
    them, so there is no outcome to learn from and nothing to predict.
    """
    outcome_by_signal = {p.signal_id: p for p in closed_positions if p.closed}

    def to_row(signal) -> TradeRow:
        breakdown = signal.score_breakdown or {}
        position = outcome_by_signal.get(signal.signal_id)
        return TradeRow(
            signal_id=signal.signal_id,
            anchor_time=_iso(signal.created_at),
            anchor_ms=signal.created_at,
            features={
                "confidence": float(signal.confidence),
                "risk_reward": float(signal.risk_reward),
                **{name: float(breakdown.get(name, 0.0)) for name in FEATURE_COLUMNS[2:]},
            },
            categories={
                "wave_label": getattr(signal.wave_label, "value", str(signal.wave_label)),
                "side": getattr(signal.side, "value", str(signal.side)),
                "entry_stage": getattr(signal.entry_stage, "value", str(signal.entry_stage)),
                "timeframe": timeframe,
            },
            won=(position.realized_pnl > 0) if position else None,
            closed_ms=position.closed_at if position else None,
        )

    accepted = [s for s in signals if s.decision == "SIGNAL_ACCEPTED"]
    context = [to_row(s) for s in accepted if s.signal_id in outcome_by_signal]
    predict = [to_row(s) for s in accepted if s.signal_id not in outcome_by_signal]
    return context, predict
