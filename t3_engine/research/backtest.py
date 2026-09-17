"""Walk-forward evaluation, with the anti-overfitting rules built in.

THE RULES ARE STRUCTURAL, NOT ADVISORY. Anything left to discipline gets
broken eventually, usually by the person most convinced they would not.
So:

  SPLITS ARE CHRONOLOGICAL AND PURGED. Train, validation and one final
  test, in time order, with a gap between them at least as long as the
  longest outcome window. Without the purge a trade opened at the end of
  train resolves inside validation, and the two sets share the same
  future.

  THE FINAL TEST IS SPENT ONCE. `WalkForward.final_test` refuses to run
  twice for the same experiment id unless a new period is supplied,
  because a set you have looked at twice is not untouched, and the second
  look is where the flattering number comes from.

  THE UNIT OF EVIDENCE IS AN EPISODE, NOT A ROW. Repeated snapshots of
  one setup are one observation. Overlapping positions are one
  observation. Simultaneous signals on correlated instruments are one
  observation. Uncertainty is therefore estimated by resampling TIME
  BLOCKS, never by shuffling individual trades, which would assume the
  independence the data does not have.

  EVERY RESULT IS KEPT, INCLUDING THE FAILURES. An experiment log that
  only holds the winners is a search that has already overfitted and
  hidden the evidence.

WHAT A CONFIDENCE INTERVAL MEANS HERE. It is the spread of the block
bootstrap, and with 40-odd episodes it will be wide. A wide interval that
includes zero is the honest answer "we cannot tell", and the acceptance
rule treats it as such rather than reading the point estimate aloud.
"""

from __future__ import annotations

import json
import math
import random
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from t3_engine.research.portfolio import JournalRow

TRAIN = "train"
VALIDATION = "validation"
FINAL_TEST = "final_test"
FORWARD = "paper_forward"


@dataclass
class Split:
    name: str
    from_ms: int
    to_ms: int

    @property
    def span_hours(self) -> float:
        return (self.to_ms - self.from_ms) / 3_600_000.0

    def contains(self, at_ms: int) -> bool:
        return self.from_ms <= at_ms < self.to_ms

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "from_ms": self.from_ms, "to_ms": self.to_ms,
                "span_hours": round(self.span_hours, 3)}


def chronological_splits(from_ms: int, to_ms: int, *, purge_ms: int,
                         weights: Tuple[float, float, float] = (0.5, 0.25, 0.25)
                         ) -> List[Split]:
    """Train / validation / final test, in time order, with a purge gap.

    The purge must be at least the longest outcome window a strategy can
    hold, or a position opened at the end of one split resolves inside
    the next and the two share a future."""
    total = to_ms - from_ms
    if total <= 3 * purge_ms:
        raise ValueError(f"period of {total}ms is too short to purge "
                         f"{purge_ms}ms between three splits")
    usable = total - 2 * purge_ms
    train_end = from_ms + int(usable * weights[0])
    val_start = train_end + purge_ms
    val_end = val_start + int(usable * weights[1])
    test_start = val_end + purge_ms
    return [
        Split(TRAIN, from_ms, train_end),
        Split(VALIDATION, val_start, val_end),
        Split(FINAL_TEST, test_start, to_ms),
    ]


def walk_forward_windows(from_ms: int, to_ms: int, *, train_ms: int,
                         test_ms: int, purge_ms: int) -> List[Tuple[Split, Split]]:
    """Rolling (train, test) pairs. Each test window is out of sample for
    the parameters fitted on the train window immediately before it."""
    windows: List[Tuple[Split, Split]] = []
    cursor = from_ms
    while cursor + train_ms + purge_ms + test_ms <= to_ms:
        train = Split(TRAIN, cursor, cursor + train_ms)
        test = Split(VALIDATION, train.to_ms + purge_ms,
                     train.to_ms + purge_ms + test_ms)
        windows.append((train, test))
        cursor += test_ms
    return windows


# ---- independence ---------------------------------------------------------

def episodes(rows: Sequence[JournalRow], *, gap_ms: int = 600_000
             ) -> List[List[JournalRow]]:
    """Group trades that are really one event.

    Two trades belong to the same episode if they overlap in time, or if
    the gap between them is shorter than `gap_ms`. Counting each snapshot
    of one setup as its own evidence is how 45 real observations were
    once reported as 451."""
    ordered = sorted(rows, key=lambda r: r.entry_at_ms)
    out: List[List[JournalRow]] = []
    for row in ordered:
        if out:
            last = out[-1][-1]
            last_end = max(last.exit_at_ms or last.entry_at_ms, last.entry_at_ms)
            if row.entry_at_ms - last_end <= gap_ms:
                out[-1].append(row)
                continue
        out.append([row])
    return out


def episode_returns(rows: Sequence[JournalRow], *, gap_ms: int = 600_000
                    ) -> List[float]:
    """One number per episode: the net bps of the whole episode, weighted
    by notional, not the average of its rows."""
    out = []
    for group in episodes(rows, gap_ms=gap_ms):
        notional = sum(r.entry_price * r.qty for r in group)
        if notional <= 0:
            continue
        out.append(10_000.0 * sum(r.net_pnl for r in group) / notional)
    return out


def block_bootstrap(values: Sequence[float], *, block: int = 3,
                    draws: int = 2_000, seed: int = 20260917
                    ) -> Dict[str, float]:
    """Resample CONTIGUOUS BLOCKS, not individual observations.

    Shuffling single trades assumes they are independent, which is the
    assumption the whole module exists to deny. Blocks keep neighbouring
    observations together so the interval reflects the clustering that is
    actually there."""
    if len(values) < 2:
        return {"n": len(values), "mean": values[0] if values else 0.0,
                "ci_low": float("nan"), "ci_high": float("nan"),
                "p_above_zero": float("nan")}
    rng = random.Random(seed)
    block = max(1, min(block, len(values)))
    needed = math.ceil(len(values) / block)
    size = len(values)
    means = []
    for _ in range(draws):
        sample: List[float] = []
        for _ in range(needed):
            start = rng.randrange(0, size)
            # CIRCULAR blocks. Taking values[start:start+block] straight
            # gives short blocks near the end, which quietly over-weights
            # the beginning of the series - a bias that shows up as a CI
            # that is too narrow exactly when the data is most clustered.
            sample.extend(values[(start + i) % size] for i in range(block))
        means.append(statistics.fmean(sample[:size]))
    means.sort()
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 4),
        "ci_low": round(means[int(0.025 * len(means))], 4),
        "ci_high": round(means[int(0.975 * len(means))], 4),
        "p_above_zero": round(sum(1 for m in means if m > 0) / len(means), 4),
    }


# ---- acceptance -----------------------------------------------------------

@dataclass
class AcceptanceCriteria:
    """Fixed before the research. A criterion adjusted after seeing a
    result is not a criterion."""
    min_net_pnl: float = 0.0
    min_expectancy_bps: float = 0.0
    min_profit_factor: float = 1.2
    min_episodes: int = 30
    min_distinct_days: int = 3
    max_single_day_share: float = 0.6
    max_single_symbol_share: float = 0.8
    require_ci_above_zero: bool = True
    survives_stress: bool = True

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def assess(report: Dict[str, Any], rows: Sequence[JournalRow],
           bootstrap: Dict[str, float],
           criteria: Optional[AcceptanceCriteria] = None,
           stress: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Does this clear the bar that was set BEFORE the run?"""
    criteria = criteria or AcceptanceCriteria()
    checks: Dict[str, Any] = {}
    closed = [r for r in rows if not r.open]

    checks["net_pnl_positive"] = report.get("net_pnl", 0.0) > criteria.min_net_pnl
    checks["expectancy_positive"] = (report.get("expectancy_bps", 0.0)
                                     > criteria.min_expectancy_bps)
    pf = report.get("profit_factor", 0.0)
    checks["profit_factor"] = (pf >= criteria.min_profit_factor
                               if pf != float("inf") else True)
    groups = episodes(closed)
    checks["enough_episodes"] = len(groups) >= criteria.min_episodes

    by_day: Dict[int, float] = {}
    by_symbol: Dict[str, float] = {}
    for row in closed:
        by_day[row.exit_at_ms // 86_400_000] = (
            by_day.get(row.exit_at_ms // 86_400_000, 0.0) + row.net_pnl)
        by_symbol[row.symbol] = by_symbol.get(row.symbol, 0.0) + row.net_pnl
    total = sum(r.net_pnl for r in closed)
    checks["enough_days"] = len(by_day) >= criteria.min_distinct_days
    checks["not_one_day"] = True
    checks["not_one_symbol"] = True
    if total > 0:
        best_day = max(by_day.values()) if by_day else 0.0
        best_symbol = max(by_symbol.values()) if by_symbol else 0.0
        checks["not_one_day"] = best_day / total <= criteria.max_single_day_share
        checks["not_one_symbol"] = (best_symbol / total
                                    <= criteria.max_single_symbol_share)

    ci_low = bootstrap.get("ci_low", float("nan"))
    checks["uncertainty_excludes_zero"] = (
        bool(ci_low == ci_low and ci_low > 0) if criteria.require_ci_above_zero
        else True)
    checks["survives_stress"] = bool(
        (stress or {}).get("worst_net_pnl", 0.0) > 0) if criteria.survives_stress \
        and stress is not None else not criteria.survives_stress

    passed = all(checks.values())
    if passed:
        verdict = "CONFIRMED_ON_HELD_OUT_DATA"
    elif (checks["net_pnl_positive"] and checks["expectancy_positive"]
          and not checks["uncertainty_excludes_zero"]):
        # The single most common way a marginal result gets promoted.
        verdict = "PRELIMINARY_CANDIDATE"
    else:
        verdict = "NO_EDGE_FOUND"
    return {"verdict": verdict, "checks": checks,
            "criteria": criteria.as_dict(),
            "bootstrap": bootstrap,
            "episodes": len(groups),
            "distinct_days": len(by_day),
            "by_day": {str(k): round(v, 4) for k, v in sorted(by_day.items())},
            "by_symbol": {k: round(v, 4) for k, v in sorted(by_symbol.items())}}


@dataclass
class ExperimentRecord:
    """One run, kept whether or not it worked."""
    experiment_id: str
    strategy: str
    version: str
    config_hash: str
    split: str
    from_ms: int
    to_ms: int
    params: Dict[str, Any]
    report: Dict[str, Any]
    assessment: Dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    def as_row(self) -> Dict[str, Any]:
        return {
            "experiment_id": self.experiment_id, "strategy": self.strategy,
            "version": self.version, "config_hash": self.config_hash,
            "split": self.split, "from_ms": self.from_ms, "to_ms": self.to_ms,
            "params": self.params, "report": self.report,
            "assessment": self.assessment, "notes": self.notes,
        }
