"""Trading a count the AI produced, with the engine's own rules.

The live chart is the AI's read of the market and nothing else - no
engine scenario, no engine zigzag. So the paper trading on that chart has
to come from the same place: the structures the agent submitted, traded by
the rules that are already in this codebase and already work (signal
scoring, the wave-3/4/5/C entry plans, risk sizing, the stop and targets).

What this module does NOT do is give the model any more authority than it
had. Two things stay exactly where they were:

  - Every structure here has already been through
    elliott_engine/external_count.py's hard rules. A count that breaks one
    never reaches the cache, so it never reaches this function.
  - Prices are never taken from the model. The entry, stop and targets are
    computed by fibonacci/signal_engine from the wave geometry, the same
    as for an engine-built scenario. The model says "these swings are
    waves 1 through 4"; everything downstream of that is arithmetic.

The one genuinely new thing is the DEVELOPING wave. A saved count is a
list of COMPLETED waves plus a projection naming what comes next. Nothing
is tradeable about a completed wave - the trade is in the one now
forming - so `scenario_from_analysis` appends an open wave for the
projection's `next_label`, anchored at the last completed wave's end. That
open wave is what `current_wave` returns and what the entry plans key off.

Scoring deliberately reuses the engine's own formulas (the same
`score_fibonacci`, the same `0.5 + 0.1 * len(waves)` validity, the same
invalidation level) rather than inventing numbers for AI counts. An AI
scenario and an engine scenario are then on one scale, and a confidence
threshold means the same thing for both.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from t3_engine.common.models import Scenario, Wave, next_id
from t3_engine.common.types import Direction, Timeframe, WaveLabel, WaveStatus
from t3_engine.elliott_engine.scenario import (
    ScenarioEngine,
    _next_label,
    build_subwaves,
    score_fibonacci,
)

# Structures whose waves carry the labels the entry plans know how to
# trade. A triangle or a flat is a real, validated structure and is drawn,
# but signal_engine has no entry plan for its legs - offering one would
# mean inventing a rule this project does not have.
TRADEABLE_STRUCTURES = ("IMPULSE", "DIAGONAL", "ZIGZAG")


def _wave_from_payload(data: Dict[str, Any], degree: Timeframe) -> Optional[Wave]:
    """One saved wave dict back into a Wave.

    Times in the saved payload are in SECONDS (they are chart times, fed
    straight to the charting library); everything inside the engine works
    in milliseconds, so they are converted here rather than anywhere a
    caller might forget."""
    try:
        label = WaveLabel(str(data["label"]))
        start_price = float(data["start_price"])
        end_price = float(data["end_price"])
        start_ms = int(data["start_time"]) * 1000
        end_ms = int(data["end_time"]) * 1000
    except (KeyError, TypeError, ValueError):
        return None
    direction = Direction.UP if end_price >= start_price else Direction.DOWN
    return Wave(
        wave_id=next_id("ai-wave"), parent_wave_id=None, degree=degree, label=label,
        direction=direction, start_timestamp=start_ms, end_timestamp=end_ms,
        start_price=start_price, end_price=end_price,
        high=max(start_price, end_price), low=min(start_price, end_price),
        status=WaveStatus.CONFIRMED,
    )


def _developing_wave(previous: Wave, label: WaveLabel, target: Optional[float],
                     degree: Timeframe) -> Wave:
    """The wave now forming, anchored at the end of the last completed one.

    It has no end yet - end == start - which is exactly what it is: a wave
    that has begun and not finished. Its direction comes from where the
    projection says price is going, falling back to alternation (the next
    wave goes the other way) when there is no target."""
    if target is not None and target != previous.end_price:
        direction = Direction.UP if target > previous.end_price else Direction.DOWN
    else:
        direction = Direction.DOWN if previous.direction == Direction.UP else Direction.UP
    return Wave(
        wave_id=next_id("ai-wave"), parent_wave_id=None, degree=degree, label=label,
        direction=direction, start_timestamp=previous.end_timestamp,
        end_timestamp=previous.end_timestamp, start_price=previous.end_price,
        end_price=previous.end_price, high=previous.end_price, low=previous.end_price,
        status=WaveStatus.DEVELOPING,
    )


def scenario_from_analysis(analysis: Dict[str, Any], degree: Timeframe) -> Optional[Scenario]:
    """A tradeable Scenario built from a saved AI analysis, or None.

    None is returned - not an empty scenario - whenever there is nothing
    to trade: no accepted structure, no structure of a kind the entry
    plans cover, or a count with no wave now developing. A scenario that
    exists but can never produce a trade is harder to read than no
    scenario at all."""
    if not analysis:
        return None
    accepted = analysis.get("accepted") or []
    tradeable = [s for s in accepted
                 if any(str(s.get("structure", "")).startswith(kind)
                        for kind in TRADEABLE_STRUCTURES)]
    if not tradeable:
        return None

    # The LAST such structure: a count covering a long history has older
    # structures behind it, and the trade is always in the newest one.
    waves: List[Wave] = []
    for payload in (tradeable[-1].get("waves") or []):
        wave = _wave_from_payload(payload, degree)
        if wave is not None:
            waves.append(wave)
    if not waves:
        return None
    waves.sort(key=lambda w: w.start_timestamp)

    projection = analysis.get("projection") or {}
    next_label: Optional[WaveLabel] = None
    raw_label = projection.get("next_label")
    if raw_label is not None:
        try:
            next_label = WaveLabel(str(raw_label))
        except ValueError:
            next_label = None
    if next_label is None:
        next_label = _next_label(waves[-1].label)
    if next_label is None:
        return None                 # the count is complete: nothing is forming

    primary = next((t.get("price") for t in (projection.get("targets") or [])
                    if t.get("primary")), None)
    waves.append(_developing_wave(waves[-1], next_label,
                                  float(primary) if primary is not None else None, degree))

    direction = waves[-1].direction
    scenario = Scenario(
        scenario_id=next_id("ai-scenario"),
        degree=degree,
        waves=waves,
        # The engine's own formulas, so an AI count and an engine count are
        # scored on one scale and a confidence threshold means one thing.
        elliott_validity=min(1.0, 0.5 + 0.1 * len(waves)),
        fib_score=score_fibonacci(waves),
        status=WaveStatus.DEVELOPING,
        invalidation=ScenarioEngine._invalidation_level(waves, direction),
        next_expected_label=_next_label(next_label),
    )
    return scenario


def analysis_fingerprint(analysis: Optional[Dict[str, Any]]) -> str:
    """What identifies one saved count, for "has this changed since I last
    looked at it". Built from when it was made and what it says, so a
    re-analysis that produced the same count does not re-trigger an entry
    evaluation."""
    if not analysis:
        return ""
    accepted = analysis.get("accepted") or []
    labels = [w.get("label") for s in accepted for w in (s.get("waves") or [])]
    projection = analysis.get("projection") or {}
    return f"{analysis.get('analysed_at', '')}|{'-'.join(str(l) for l in labels)}" \
           f"|{projection.get('next_label', '')}"


# Only motive waves subdivide into a five-wave count; the engine's own
# subwave pass uses the same three labels for the same reason (see
# backtest/engine.py's _MOTIVE_LABELS_FOR_SUBWAVES).
_SUBDIVIDABLE = (WaveLabel.W1, WaveLabel.W3, WaveLabel.W5)
_MIN_CANDLES_FOR_SUBWAVES = 6


def subwaves_for(scenario: Optional[Scenario], candles: List[Any],
                 deviation_pct: float = 0.5) -> List[Wave]:
    """Subdivide the agent's motive waves, with the engine's own routine.

    In live mode the chart is the agent's read of the market, which has to
    mean ALL of it: the subwave detail under waves 1/3/5 is derived from
    the agent's count rather than from the engine's parallel one, or the
    finer degree on screen would be describing a different reading than the
    labels above it.

    The subdivision itself is not the agent's work and is not asked of it -
    it is `build_subwaves`, the same ZigZag-at-a-finer-deviation pass
    graded by the same hard rules that the engine's own counts go through.
    Only COMPLETED waves are subdivided: the developing one has no end yet,
    so there is nothing inside it to count."""
    if scenario is None or not candles:
        return []
    out: List[Wave] = []
    for wave in scenario.waves:
        if wave.label not in _SUBDIVIDABLE or wave.status == WaveStatus.DEVELOPING:
            continue
        span = [c for c in candles
                if wave.start_timestamp <= c.open_time <= wave.end_timestamp]
        if len(span) < _MIN_CANDLES_FOR_SUBWAVES:
            continue
        built = build_subwaves(span, wave, deviation_pct)
        out.extend(built.get("waves") or [])
    return sorted(out, key=lambda w: w.start_timestamp)


def grid_scenario(scenario: Optional[Scenario]) -> Optional[Scenario]:
    """The same count, shaped the way the Fibonacci helper reads a count.

    Two different conventions meet here. A tradeable scenario carries the
    DEVELOPING wave in `waves`, because `current_wave` is what the entry
    plans key off. `fibonacci_levels_for_scenario` reads the opposite
    convention - completed waves in `waves`, and the wave being projected
    in `next_expected_label`.

    Handing it the trading shape asks for the grid of the wave AFTER the
    one now forming, which for a developing wave 5 is wave A - and there
    is deliberately no formula for A, so the answer came back empty. This
    re-shapes rather than papering over it: drop the developing wave from
    the list and name it as the one expected."""
    if scenario is None or len(scenario.waves) < 2:
        return None
    completed = [w for w in scenario.waves if w.status != WaveStatus.DEVELOPING]
    developing = next((w for w in scenario.waves if w.status == WaveStatus.DEVELOPING), None)
    if not completed or developing is None:
        return None
    return Scenario(
        scenario_id=scenario.scenario_id, degree=scenario.degree, waves=completed,
        probability=scenario.probability, elliott_validity=scenario.elliott_validity,
        fib_score=scenario.fib_score, invalidation=scenario.invalidation,
        status=scenario.status, next_expected_label=developing.label,
    )
