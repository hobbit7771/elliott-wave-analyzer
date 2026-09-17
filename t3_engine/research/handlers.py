"""What each job kind actually does.

Every handler takes a spec dict and returns a JSON-serialisable result.
They are deliberately small: the thinking lives in `strategies`,
`execution` and `backtest`, and this file only wires a request to them
and keeps the box from falling over while it runs.

MEMORY IS THE BINDING CONSTRAINT ON A FREE BOX, so segments are decoded
one at a time and events are streamed rather than gathered into a list.
A handler that materialises a day of book updates is a handler that gets
OOM-killed halfway through and leaves a job marked running forever.
"""

from __future__ import annotations

import gc
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from t3_engine.research import backtest as bt
from t3_engine.research import dataset as ds
from t3_engine.research import jobs
from t3_engine.research.book import BookState, Trade
from t3_engine.research.capture import TABLE_CAPTURES, decode_segment, verify_segment
from t3_engine.research.execution import InstrumentSpec, LatencyModel, QueueModel
from t3_engine.research.portfolio import RiskLimits
from t3_engine.research.runner import RunnerConfig, StrategyRunner
from t3_engine.research.strategies import ALL_STRATEGIES

SEGMENT_PAGE = 25


def _rest():
    from t3_engine.database import supabase_rest
    return supabase_rest


# ---- loading --------------------------------------------------------------

def segment_index(symbol: Optional[str] = None, from_ms: int = 0,
                  to_ms: int = 0) -> List[Dict[str, Any]]:
    """Segment metadata WITHOUT the payloads.

    Selecting `payload` here would pull the whole capture into memory to
    answer "what is there", which is the opposite of the point."""
    columns = ("segment_id,session_id,symbol,seq,frames,dropped_before,"
               "first_exchange_ms,last_exchange_ms,first_recv_ms,last_recv_ms,"
               "topics,raw_bytes,stored_bytes,sha256")
    params: List[Tuple[str, str]] = [("select", columns), ("order", "seq.asc"),
                                     ("limit", "20000")]
    if symbol:
        params.append(("symbol", f"like.*{symbol.upper()}*"))
    if from_ms:
        params.append(("last_exchange_ms", f"gte.{int(from_ms)}"))
    if to_ms:
        params.append(("first_exchange_ms", f"lte.{int(to_ms)}"))
    rows = _rest()._request("GET", TABLE_CAPTURES, params=params)
    return rows if isinstance(rows, list) else []


def stream_frames(segment_ids: Sequence[str], *, verify: bool = True,
                  yield_every: int = jobs.YIELD_EVERY_FRAMES
                  ) -> Iterator[Dict[str, Any]]:
    """One segment at a time, decoded, yielded, released.

    The sleep is not politeness, it is the reason the ingest thread keeps
    up while this runs. Without it a long job starves the socket and puts
    real gaps in the data being recorded."""
    seen = 0
    for start in range(0, len(segment_ids), SEGMENT_PAGE):
        chunk = list(segment_ids[start:start + SEGMENT_PAGE])
        rows = _rest()._request(
            "GET", TABLE_CAPTURES,
            params=[("select", "segment_id,seq,payload,sha256,frames"),
                    ("segment_id", f"in.({','.join(chunk)})"),
                    ("order", "seq.asc")])
        for row in rows or []:
            if verify:
                ok, why = verify_segment(row)
                if not ok:
                    raise ValueError(f"segment {row['segment_id']}: {why}")
            for frame in decode_segment(row["payload"]):
                yield frame
                seen += 1
                if yield_every and seen % yield_every == 0:
                    time.sleep(jobs.YIELD_SECONDS)
            row["payload"] = ""
        del rows
        gc.collect()


# ---- handlers -------------------------------------------------------------

def handle_manifest(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Measure what the capture IS, not what it is called."""
    symbol = spec.get("symbol")
    index = segment_index(symbol, int(spec.get("from_ms") or 0),
                          int(spec.get("to_ms") or 0))
    if not index:
        return {"error": "no segments match", "segments": 0}

    ids = [r["segment_id"] for r in index]
    cap = int(spec.get("max_frames") or jobs.MAX_FRAMES_PER_JOB)
    frames: List[Dict[str, Any]] = []
    for frame in stream_frames(ids):
        frames.append(frame)
        if len(frames) >= cap:
            break

    manifest = ds.describe(
        frames,
        name=spec.get("name") or f"research_captures/{symbol or 'all'}",
        source=f"Supabase {TABLE_CAPTURES}, {len(index)} segments",
        provenance=("recorded live from Bybit's public linear websocket by "
                    "t3_engine.research.capture running on Render; book "
                    "deltas merged into exact 100ms windows, trades and "
                    "liquidations kept one for one"),
        segments=len(index),
        dropped=sum(int(r.get("dropped_before") or 0) for r in index))
    out = manifest.as_dict()
    out["markdown"] = manifest.render()
    out["segments_indexed"] = len(index)
    out["stored_mb"] = round(sum(int(r.get("stored_bytes") or 0)
                                 for r in index) / 1e6, 3)
    out["frames_examined"] = len(frames)
    out["frames_total_indexed"] = sum(int(r.get("frames") or 0) for r in index)
    return out


def _instrument(symbol: str, spec: Dict[str, Any]) -> InstrumentSpec:
    return InstrumentSpec(
        symbol=symbol,
        tick_size=float(spec.get("tick_size") or 0.001),
        qty_step=float(spec.get("qty_step") or 0.1),
        min_qty=float(spec.get("min_qty") or 0.1),
        min_notional=float(spec.get("min_notional") or 5.0))


def _runner_config(symbol: str, spec: Dict[str, Any]) -> RunnerConfig:
    latency = spec.get("latency") or {}
    queue = spec.get("queue") or {}
    return RunnerConfig(
        symbol=symbol,
        spec=_instrument(symbol, spec.get("instrument") or {}),
        latency=LatencyModel(send_ms=float(latency.get("send_ms", 120.0)),
                             ack_ms=float(latency.get("ack_ms", 60.0)),
                             cancel_ms=float(latency.get("cancel_ms", 120.0))),
        queue=QueueModel(queue_factor=float(queue.get("queue_factor", 1.0)),
                         cancel_helps=bool(queue.get("cancel_helps", False))),
        limits=RiskLimits(**(spec.get("limits") or {})))


def run_one(spec: Dict[str, Any]) -> Tuple[StrategyRunner, Dict[str, Any]]:
    """Drive one strategy over one window. The core of every other job."""
    symbol = str(spec["symbol"]).upper()
    name = str(spec["strategy"])
    if name not in ALL_STRATEGIES:
        raise ValueError(f"unknown strategy {name!r}")
    strategy = ALL_STRATEGIES[name](spec.get("params") or {})
    config = _runner_config(symbol, spec)
    runner = StrategyRunner(strategy, config)

    index = segment_index(symbol, int(spec.get("from_ms") or 0),
                          int(spec.get("to_ms") or 0))
    if not index:
        raise ValueError("no segments match the requested window")
    ids = [r["segment_id"] for r in index]

    from_ms = int(spec.get("from_ms") or 0)
    to_ms = int(spec.get("to_ms") or 0)
    cap = int(spec.get("max_frames") or jobs.MAX_FRAMES_PER_JOB)

    processed = 0
    last_ms = 0
    for event in ds.events_from_frames(_capped(stream_frames(ids), cap), symbol):
        if from_ms and event.exchange_ms and event.exchange_ms < from_ms:
            continue
        if to_ms and event.exchange_ms and event.exchange_ms > to_ms:
            break
        last_ms = event.recv_ms or last_ms
        if event.kind == "book":
            runner.on_book(event.payload)
        elif event.kind == "trade":
            runner.on_trade(event.payload)
        elif event.kind == "liquidation":
            notional, side = event.payload
            runner.on_liquidation(event.recv_ms, notional, side)
        processed += 1

    runner.finalise(last_ms)
    report = runner.report()
    report["events_processed"] = processed
    report["segments_used"] = len(index)
    report["window"] = {"from_ms": from_ms, "to_ms": to_ms}
    return runner, report


def _capped(frames: Iterator[Dict[str, Any]], cap: int) -> Iterator[Dict[str, Any]]:
    for index, frame in enumerate(frames):
        if index >= cap:
            return
        yield frame


def handle_backtest(spec: Dict[str, Any]) -> Dict[str, Any]:
    runner, report = run_one(spec)
    rows = [r for r in runner.portfolio.journal if not r.open]
    returns = bt.episode_returns(rows)
    bootstrap = bt.block_bootstrap(returns)
    assessment = bt.assess(report, rows, bootstrap,
                           bt.AcceptanceCriteria(**(spec.get("criteria") or {})))
    record = bt.ExperimentRecord(
        experiment_id=str(spec.get("experiment_id")
                          or f"{spec['strategy']}-{spec.get('split', 'adhoc')}-"
                             f"{int(time.time())}"),
        strategy=spec["strategy"], version=runner.strategy.version,
        config_hash=runner.portfolio.config_hash,
        split=str(spec.get("split") or "adhoc"),
        from_ms=int(spec.get("from_ms") or 0), to_ms=int(spec.get("to_ms") or 0),
        params=runner.strategy.params, report=report, assessment=assessment,
        notes=str(spec.get("notes") or ""))
    jobs.record_experiment(record)
    return {"experiment_id": record.experiment_id, "report": report,
            "assessment": assessment,
            "journal_sample": [r.as_dict() for r in rows[:20]]}


def handle_ablation(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Switch one filter off at a time. Complexity that survives only
    because nobody tested it is complexity that breaks out of sample."""
    name = str(spec["strategy"])
    strategy_cls = ALL_STRATEGIES[name]
    base_params = dict(spec.get("params") or {})
    variants: Dict[str, Dict[str, Any]] = {"baseline": {}}
    variants.update(strategy_cls(base_params).ablations())

    out: Dict[str, Any] = {"strategy": name, "variants": {}}
    for variant, override in variants.items():
        params = dict(base_params)
        params.update(override)
        sub = dict(spec)
        sub["params"] = params
        sub["experiment_id"] = (f"{name}-ablation-{variant}-"
                                f"{int(spec.get('from_ms') or 0)}")
        sub["split"] = f"ablation:{variant}"
        sub["notes"] = f"ablation: {variant}"
        try:
            result = handle_backtest(sub)
            report = result["report"]
            out["variants"][variant] = {
                "trades": report.get("trades"),
                "net_pnl": report.get("net_pnl"),
                "expectancy_bps": report.get("expectancy_bps"),
                "profit_factor": report.get("profit_factor"),
                "verdict": result["assessment"]["verdict"],
            }
        except Exception as exc:
            out["variants"][variant] = {"error": f"{type(exc).__name__}: {exc}"}
        time.sleep(0.05)
    return out


def handle_stress(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Would it survive execution being worse than assumed?

    A result that only exists at the optimistic queue model or at
    laboratory latency has not been shown to work; it has been shown to
    need conditions nobody measured."""
    scenarios = spec.get("scenarios") or [
        {"name": "baseline"},
        {"name": "latency x2", "latency": {"send_ms": 240, "ack_ms": 120,
                                           "cancel_ms": 240}},
        {"name": "latency x4", "latency": {"send_ms": 480, "ack_ms": 240,
                                           "cancel_ms": 480}},
        {"name": "fees +50%", "fee_multiplier": 1.5},
        {"name": "optimistic queue", "queue": {"queue_factor": 0.5,
                                               "cancel_helps": True}},
        {"name": "participation 10%", "max_participation": 0.10},
    ]
    out: Dict[str, Any] = {"strategy": spec.get("strategy"), "scenarios": {}}
    worst = None
    for scenario in scenarios:
        sub = dict(spec)
        for key in ("latency", "queue"):
            if key in scenario:
                sub[key] = scenario[key]
        sub["experiment_id"] = (f"{spec['strategy']}-stress-"
                                f"{scenario['name'].replace(' ', '_')}-"
                                f"{int(spec.get('from_ms') or 0)}")
        sub["split"] = f"stress:{scenario['name']}"
        sub["notes"] = f"stress: {scenario['name']}"
        try:
            result = handle_backtest(sub)
            net = result["report"].get("net_pnl", 0.0)
            out["scenarios"][scenario["name"]] = {
                "net_pnl": net,
                "trades": result["report"].get("trades"),
                "expectancy_bps": result["report"].get("expectancy_bps"),
            }
            worst = net if worst is None else min(worst, net)
        except Exception as exc:
            out["scenarios"][scenario["name"]] = {
                "error": f"{type(exc).__name__}: {exc}"}
        time.sleep(0.05)
    out["worst_net_pnl"] = worst
    return out


def build() -> Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]]:
    return {
        "manifest": handle_manifest,
        "backtest": handle_backtest,
        "ablation": handle_ablation,
        "stress": handle_stress,
    }
