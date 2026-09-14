"""Count a WHOLE history, end to end, with the engine's own rules.

The interactive analyst counts a chart by proposing structures and having
them graded. It works, and it costs a model call per step - which is why
it was run on a few hundred bars at a time and why a 1500-bar chart came
back labelled in patches.

This module does the same job without a model. It is a search, not a
guess: ZigZag pivots over the full series, then every candidate structure
that could start at each pivot handed to `validate_external_structure` -
the identical hard rules (wave 3 never the shortest, wave 4 never
overlapping wave 1, wave 2 never past the start of 1, a zigzag's B never
past the start of A) that reject a model's count. Nothing is "accepted"
here that the rule engine would not accept from anyone else.

Three things it deliberately does:

  - It picks its own deviation. A 4h chart and a 5m chart do not swing by
    the same percentage, and one hardcoded threshold labels one of them
    well and the other badly. Several are tried and the one that labels
    the most of the chart wins, which is a measurement rather than a
    preference.
  - It counts the TAIL as a partial structure. A complete 1-2-3-4-5 says
    what already happened; the forecast lives in the incomplete count
    running up to the last candle, so after the complete structures are
    placed the leftover pivots are fitted as a partial motive count and
    the wave now forming is named from it.
  - It subdivides. Waves 1, 3 and 5 of every accepted structure get their
    own i-ii-iii-iv-v pass at a finer deviation, graded by the same rules.

What it does NOT do is invent prices. Every price in the output is a
pivot's own high or low, and every target is a Fibonacci level computed
from those pivots - see analyst_tools.project_next_wave.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from t3_engine.ai_advisor.analyst_tools import coverage_of, project_next_wave, round_price
from t3_engine.common.models import Candle
from t3_engine.common.types import Direction, Timeframe, WaveLabel
from t3_engine.elliott_engine.external_count import (
    ExternalCountRejected,
    STRUCTURE_DIAGONAL_CONTRACTING,
    STRUCTURE_DIAGONAL_EXPANDING,
    STRUCTURE_FLAT,
    STRUCTURE_IMPULSE,
    STRUCTURE_TRIANGLE,
    STRUCTURE_ZIGZAG,
    validate_external_structure,
)
from t3_engine.elliott_engine.scenario import _next_label, build_subwaves, score_fibonacci
from t3_engine.market_structure.pivots import ZigZagPivotDetector

# Every reading tried at every starting pivot. The hard rules decide which
# are LEGAL; the score below decides which legal one is taken, because a
# stretch of chart frequently admits more than one and "whichever was
# tested first" is not a reason to prefer a count.
_CANDIDATES: Tuple[Tuple[str, int], ...] = (
    (STRUCTURE_IMPULSE, 5),
    (STRUCTURE_DIAGONAL_CONTRACTING, 5),
    (STRUCTURE_DIAGONAL_EXPANDING, 5),
    (STRUCTURE_TRIANGLE, 5),
    (STRUCTURE_ZIGZAG, 3),
    (STRUCTURE_FLAT, 3),
)

# What a reading is worth before its proportions are looked at. An impulse
# that satisfies the impulse rules is the strongest statement available
# about a stretch of chart and outranks everything; a flat is the weakest,
# because its only hard requirement is a deep wave B and plenty of
# geometry satisfies that by accident.
# The spread is deliberately wider than everything below can bridge: a
# five-wave reading that satisfies the motive rules explains five legs of
# chart and outranks any three-leg correction of the same stretch, and no
# amount of pretty proportion is allowed to overturn that. Proportion only
# ever decides between readings of the SAME class.
_STRUCTURE_WEIGHT = {
    STRUCTURE_IMPULSE: 2.20,
    STRUCTURE_DIAGONAL_CONTRACTING: 1.90,
    STRUCTURE_DIAGONAL_EXPANDING: 1.80,
    STRUCTURE_ZIGZAG: 1.00,
    STRUCTURE_TRIANGLE: 0.90,
    STRUCTURE_FLAT: 0.70,
}

# How much the Fibonacci proportions are allowed to move a reading. Enough
# to decide between two corrections; never enough to put a flat above an
# impulse, because that is a rule question and not a proportion one.
_FIB_WEIGHT = 0.5

# A flat is the SIDEWAYS correction. The hard rule only asks that wave B be
# deep, which a strongly directional three-leg advance can satisfy - and
# then a 20% rally carries the label "flat". So a flat is additionally
# rewarded here for actually going nowhere: net displacement against
# distance travelled. A soft preference in this search, not a new rule -
# an expanded flat with real net displacement is still legal and still
# placed when nothing else fits.
_FLATNESS_WEIGHT = 0.2


def _score_candidate(structure: str, validated) -> float:
    """How good a LEGAL reading is. The rules have already spoken by the
    time this is called; this only chooses between the survivors."""
    waves = validated.waves
    score = _STRUCTURE_WEIGHT.get(structure, 0.5) + _FIB_WEIGHT * score_fibonacci(waves)
    if structure == STRUCTURE_FLAT and waves:
        travelled = sum(w.length for w in waves)
        net = abs(waves[-1].end_price - waves[0].start_price)
        if travelled > 0:
            score += _FLATNESS_WEIGHT * (1.0 - min(1.0, net / travelled))
    return score

# The deviations tried when picking one for a chart. It has to span the
# real range: 1500 5m bars is five days of half-percent wiggles, while
# 1500 4h bars is eight months in which this instrument went from 7.34 to
# 2.65 and back. A ladder that stopped at 6% counted that 4h chart at
# minute degree and produced forty-nine structures, twenty-seven of them
# flats - a correct count of the wrong degree.
_DEVIATION_LADDER: Tuple[float, ...] = (25.0, 20.0, 16.0, 12.0, 9.0, 6.0, 4.5, 3.5,
                                        2.5, 2.0, 1.5, 1.2, 1.0, 0.8, 0.6)

# Below this a "count" is a handful of swings and the rules have had
# nothing to reject. Reported rather than dressed up.
MIN_PIVOTS_FOR_A_COUNT = 6

# What a reading has to manage before it is allowed to win on being the
# coarsest. Coverage rises monotonically as the deviation falls - the
# finest reading almost always labels the most - so "closest to the best
# coverage" is just a slow way of always choosing the finest. A FLOOR
# instead: account for three quarters of the chart, in at least a few
# separate structures, and then the largest degree that manages it wins.
MIN_USEFUL_COVERAGE = 0.75

# How many structures a top-level count of this window should come to.
# Expressed as a band because both ends are real failures, and the first
# two attempts at this rule hit one each: maximising coverage gave 49
# structures on the 4h chart (minute degree, unreadable), and then taking
# the coarsest that cleared a floor of three gave 3 structures and no
# subwaves at all (one degree too high, nothing left to subdivide).
#
# The band says a structure at the top level should span somewhere between
# a twentieth and a quarter of the window - big enough to be the chart's
# own shape, small enough that several of them tell a story.
MIN_STRUCTURES_FOR_A_DEGREE = 4
MAX_STRUCTURES_FOR_A_DEGREE = 20

# Subwaves are by definition smaller moves than the wave holding them, so
# they are looked for at a fraction of the deviation that found the parent.
SUBWAVE_DEVIATION_RATIO = 0.35
_MIN_CANDLES_FOR_SUBWAVES = 6
_SUBDIVIDABLE = (WaveLabel.W1, WaveLabel.W3, WaveLabel.W5)

_MOTIVE_LABELS = [WaveLabel.W1, WaveLabel.W2, WaveLabel.W3, WaveLabel.W4, WaveLabel.W5]


def pivots_at(candles: List[Candle], deviation_pct: float) -> List:
    detector = ZigZagPivotDetector(deviation_pct=deviation_pct)
    for index, candle in enumerate(candles):
        detector.update(index, candle)
    return detector.pivots


def _legs(start: int, count: int, labels: List[WaveLabel]) -> List[Dict[str, Any]]:
    """Consecutive pivots as leg dicts. Every wave ends where the next one
    begins, which is what makes a count a chain rather than a set."""
    return [{"label": labels[i].value, "start_pivot_index": start + i,
             "end_pivot_index": start + i + 1} for i in range(count)]


def _wave_dicts(validated) -> List[Dict[str, Any]]:
    """The engine's own waves, in the shape the rest of this codebase
    already passes around (seconds, because these are chart times)."""
    return [{"label": w.label.value, "start_time": w.start_timestamp // 1000,
             "end_time": w.end_timestamp // 1000, "start_price": w.start_price,
             "end_price": w.end_price, "direction": w.direction.value}
            for w in validated.waves]


def _labels_for(structure: str) -> List[WaveLabel]:
    """The canonical label sequence this structure must follow. Positional
    always - the search chooses where a structure starts, never which
    label goes where."""
    if structure in (STRUCTURE_ZIGZAG, STRUCTURE_FLAT):
        return [WaveLabel.A, WaveLabel.B, WaveLabel.C]
    if structure == STRUCTURE_TRIANGLE:
        return [WaveLabel.A, WaveLabel.B, WaveLabel.C, WaveLabel.D, WaveLabel.E]
    return _MOTIVE_LABELS


def _try(pivots: List, start: int, structure: str, legs_wanted: int,
         degree: Timeframe, labels: Optional[List[WaveLabel]] = None):
    """One candidate, validated. None when it does not fit or is refused."""
    labels = labels or _MOTIVE_LABELS
    if start + legs_wanted >= len(pivots):
        return None
    direction = Direction.UP if pivots[start].kind == "LOW" else Direction.DOWN
    proposed = _legs(start, legs_wanted, labels)
    try:
        validated = validate_external_structure(pivots, proposed, structure, degree,
                                                direction=direction)
    except ExternalCountRejected:
        return None
    return validated if validated.valid else None


def count_at_deviation(candles: List[Candle], degree: Timeframe,
                       deviation_pct: float) -> Dict[str, Any]:
    """Walk the whole pivot series once, placing every structure that the
    hard rules accept, then fit the leftover tail as a partial count."""
    pivots = pivots_at(candles, deviation_pct)
    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    if len(pivots) < MIN_PIVOTS_FOR_A_COUNT:
        return {"deviation_pct": deviation_pct, "pivots": pivots, "accepted": accepted,
                "rejected": rejected, "developing": None}

    index = 0
    while index + 3 < len(pivots):
        placed = None
        best_score = float("-inf")
        legal: List[Dict[str, Any]] = []
        for structure, legs_wanted in _CANDIDATES:
            validated = _try(pivots, index, structure, legs_wanted, degree,
                             _labels_for(structure))
            if validated is None:
                continue
            score = _score_candidate(structure, validated)
            legal.append({"structure": structure, "score": round(score, 3),
                          "labels": "-".join(w.label.value for w in validated.waves)})
            if score > best_score:
                best_score, placed = score, (structure, legs_wanted, validated)
        if placed is None:
            # No structure starts here. Advancing one pivot rather than
            # giving up is the difference between labelling a chart that
            # opens mid-correction and labelling nothing at all.
            index += 1
            continue
        structure, legs_wanted, validated = placed
        accepted.append({
            "structure": structure, "deviation_pct": deviation_pct,
            "valid": True, "broken_rule": None, "reason": validated.notes,
            "score": round(best_score, 3),
            # The readings that were ALSO legal here and lost on score.
            # A preference that is never shown is indistinguishable from a
            # rule, and these are preferences: the hard rules accepted
            # every one of these, and one of them was chosen.
            "alternatives": [a for a in legal
                             if a["structure"] != placed[0]][:3],
            "waves": _wave_dicts(validated), "pivot_indices": validated.pivot_indices,
            "start_pivot_index": index,
        })
        index += legs_wanted

    developing = _fit_tail(pivots, index, degree, deviation_pct)
    return {"deviation_pct": deviation_pct, "pivots": pivots, "accepted": accepted,
            "rejected": rejected, "developing": developing}


def _fit_tail(pivots: List, start: int, degree: Timeframe,
              deviation_pct: float) -> Optional[Dict[str, Any]]:
    """The incomplete structure running up to now, longest fit first.

    This is where a forecast comes from. A finished 1-2-3-4-5 is history;
    what is tradeable is the count that has reached wave 3 and is building
    wave 4. Four legs are tried, then three, then two, then one."""
    for legs_wanted in (4, 3, 2, 1):
        if start + legs_wanted >= len(pivots):
            continue
        for structure in (STRUCTURE_IMPULSE, STRUCTURE_DIAGONAL_CONTRACTING):
            validated = _try(pivots, start, structure, legs_wanted, degree, _MOTIVE_LABELS)
            if validated is None:
                continue
            waves = _wave_dicts(validated)
            return {
                "structure": structure, "deviation_pct": deviation_pct, "valid": True,
                "broken_rule": None, "reason": validated.notes, "waves": waves,
                "pivot_indices": validated.pivot_indices, "start_pivot_index": start,
                "partial": True,
            }
    return None


def _subwaves(candles: List[Candle], structures: List[Dict[str, Any]],
              degree: Timeframe, deviation_pct: float) -> List[Dict[str, Any]]:
    """Every motive wave's own i-ii-iii-iv-v, by the engine's routine.

    `build_subwaves` wants a Wave, and what is on hand here is the dict
    form, so the parent is rebuilt from it. The subdivision itself is not
    re-implemented: it is the same pass, at the same finer deviation, put
    through the same hard rules as any primary count."""
    from t3_engine.common.models import Wave, next_id
    from t3_engine.common.types import WaveStatus

    fine = max(0.05, deviation_pct * SUBWAVE_DEVIATION_RATIO)
    out: List[Dict[str, Any]] = []
    for structure in structures:
        for payload in structure.get("waves") or []:
            try:
                label = WaveLabel(str(payload["label"]))
            except (KeyError, ValueError):
                continue
            if label not in _SUBDIVIDABLE:
                continue
            start_ms = int(payload["start_time"]) * 1000
            end_ms = int(payload["end_time"]) * 1000
            span = [c for c in candles if start_ms <= c.open_time <= end_ms]
            if len(span) < _MIN_CANDLES_FOR_SUBWAVES:
                continue
            direction = (Direction.UP if payload["end_price"] >= payload["start_price"]
                         else Direction.DOWN)
            parent = Wave(
                wave_id=next_id("deep-wave"), parent_wave_id=None, degree=degree, label=label,
                direction=direction, start_timestamp=start_ms, end_timestamp=end_ms,
                start_price=float(payload["start_price"]), end_price=float(payload["end_price"]),
                high=max(payload["start_price"], payload["end_price"]),
                low=min(payload["start_price"], payload["end_price"]),
                status=WaveStatus.CONFIRMED,
            )
            built = build_subwaves(span, parent, fine)
            for sub in built.get("waves") or []:
                out.append({
                    "parent_label": label.value, "parent_start_time": start_ms // 1000,
                    "label": sub.label.value, "start_time": sub.start_timestamp // 1000,
                    "end_time": sub.end_timestamp // 1000, "start_price": sub.start_price,
                    "end_price": sub.end_price, "direction": sub.direction.value,
                })
    out.sort(key=lambda s: s["start_time"])
    return out


def _projection_for(structure: Optional[Dict[str, Any]], degree: Timeframe) -> Optional[Dict[str, Any]]:
    """Fibonacci targets for the wave the count says is forming now."""
    if not structure:
        return None
    waves = structure.get("waves") or []
    if not waves:
        return None
    try:
        last = WaveLabel(str(waves[-1]["label"]))
    except (KeyError, ValueError):
        return None
    following = _next_label(last)
    if following is None:
        return None
    return project_next_wave(waves, following.value, bar_seconds=degree.seconds)


def _waves_as_objects(structure: Dict[str, Any], degree: Timeframe) -> List[Any]:
    """The dict form back into engine Waves, so engine code can read it."""
    from t3_engine.common.models import Wave, next_id
    from t3_engine.common.types import WaveStatus

    out = []
    for payload in structure.get("waves") or []:
        try:
            label = WaveLabel(str(payload["label"]))
            start_price = float(payload["start_price"])
            end_price = float(payload["end_price"])
        except (KeyError, TypeError, ValueError):
            continue
        direction = Direction.UP if end_price >= start_price else Direction.DOWN
        out.append(Wave(
            wave_id=next_id("deep-wave"), parent_wave_id=None, degree=degree, label=label,
            direction=direction, start_timestamp=int(payload["start_time"]) * 1000,
            end_timestamp=int(payload["end_time"]) * 1000, start_price=start_price,
            end_price=end_price, high=max(start_price, end_price),
            low=min(start_price, end_price), status=WaveStatus.CONFIRMED,
        ))
    return out


def _invalidation_for(structure: Optional[Dict[str, Any]], degree: Timeframe) -> Optional[float]:
    """The price that would break this count, by the engine's own rule.

    Not a stop-loss and not a preference - it is the level at which the
    hard rules say this reading is finished and another one is needed."""
    if not structure:
        return None
    waves = _waves_as_objects(structure, degree)
    if not waves:
        return None
    from t3_engine.elliott_engine.scenario import ScenarioEngine

    level = ScenarioEngine._invalidation_level(waves, waves[-1].direction)
    # Rounded to significant figures, like every other price this app
    # prints. A full double on screen (132.15466702367488) reads as a
    # precision the pivot it came from does not have.
    return None if level is None else round_price(level)


# Where a correction after a COMPLETED five-wave move usually goes. This is
# not a Fibonacci projection of wave A - `project_next_wave` deliberately
# has no formula for A, because the length of a correction's first leg is
# not derivable from the impulse it follows, and inventing one would put a
# fabricated price on the chart. What IS textbook and IS computable is the
# zone the correction as a whole tends to reach: a retracement of the whole
# impulse, with the fourth wave of lesser degree inside it. So that zone is
# what gets reported, named for what it is.
_CORRECTION_RATIOS = (0.382, 0.5, 0.618)


def _correction_zone(structure: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not structure:
        return None
    waves = structure.get("waves") or []
    labels = [str(w.get("label")) for w in waves]
    if labels[:5] != ["1", "2", "3", "4", "5"]:
        return None
    from t3_engine.fibonacci.calculator import retracement_levels

    start = float(waves[0]["start_price"])
    end = float(waves[4]["end_price"])
    levels = retracement_levels(start, end, list(_CORRECTION_RATIOS))
    return {
        "basis": "retracement of the completed 1-5 move",
        "from_price": round_price(end),
        "wave_four_low": round_price(float(waves[3]["end_price"])),
        "levels": [{"ratio": lvl.ratio, "price": round_price(lvl.price)} for lvl in levels],
    }


def build_count(candles: List[Candle], degree: Timeframe, symbol: str = "") -> Dict[str, Any]:
    """The full markup of one chart: structures, subwaves, forecast.

    The deviation is chosen by measurement - every rung of the ladder is
    counted and the one that LABELS THE MOST of the chart wins, ties going
    to the finer reading (more structure, same coverage). That replaces a
    constant that would have been right for one timeframe and wrong for
    the other three."""
    if len(candles) < 30:
        return {"error": f"{len(candles)} candles is too short a history to count",
                "accepted": [], "rejected": [], "subwaves": [], "pivots": [],
                "coverage": {"covered_fraction": 0.0, "gaps": []}, "projection": None}

    attempts: List[Dict[str, Any]] = []
    for deviation in _DEVIATION_LADDER:
        result = count_at_deviation(candles, degree, deviation)
        structures = result["accepted"] + ([result["developing"]] if result["developing"] else [])
        cover = coverage_of(candles, structures)
        scores = [a.get("score") or 0.0 for a in result["accepted"]]
        attempts.append({
            "deviation_pct": deviation,
            "pivots": len(result["pivots"]),
            "structures": len(result["accepted"]),
            "covered_fraction": cover["covered_fraction"],
            # How well the rules RECOGNISED what is here, averaged over the
            # structures placed. A reading that comes back as a chain of
            # flats scores low for a reason: a flat is what the search
            # falls back to when nothing stronger fits, so a low mean is
            # the measurement of "this degree is not where the structure
            # is" - see _score_candidate for the weights.
            "mean_score": (sum(scores) / len(scores)) if scores else 0.0,
            "result": result,
            "coverage": cover,
        })

    scored = [a for a in attempts if a["structures"]]
    if not scored:
        best = max(attempts, key=lambda a: a["pivots"])
    else:
        # The LARGEST degree that still accounts for the chart. Chasing
        # maximum coverage instead chops one cycle-degree impulse into a
        # dozen minute-degree ones, because a finer reading always labels
        # a little more; counting the biggest degree the data supports and
        # subdividing underneath it is the order Elliott is done in, and
        # the subdivision happens below anyway.
        usable = [a for a in scored
                  if a["covered_fraction"] >= MIN_USEFUL_COVERAGE
                  and MIN_STRUCTURES_FOR_A_DEGREE <= a["structures"]
                  <= MAX_STRUCTURES_FOR_A_DEGREE]
        if usable:
            # Best-recognised first, coarsest to break a tie. Rounding the
            # mean to one decimal is what makes it a tie-break rather than
            # an override: two readings of comparable quality defer to the
            # higher degree, and only a materially better-recognised count
            # pulls the choice finer. Taking the coarsest outright put 7
            # flats and 1 impulse on the 4h chart when one rung down had
            # ten structures the rules could actually name.
            best = max(usable, key=lambda a: (round(a["mean_score"], 1), a["deviation_pct"]))
        else:
            # Nothing cleared the floor, so there is no degree to prefer -
            # take the reading that labels the most and say so through the
            # coverage figure that travels with it.
            best = max(scored, key=lambda a: (round(a["covered_fraction"], 3), a["structures"]))

    result = best["result"]
    deviation = best["deviation_pct"]
    developing = result["developing"]
    accepted = list(result["accepted"])
    if developing:
        accepted.append(developing)

    subwaves = _subwaves(candles, accepted, degree, deviation)
    projection = _projection_for(developing or (accepted[-1] if accepted else None), degree)

    return {
        "symbol": symbol,
        "timeframe": degree.value,
        "deviation_pct": deviation,
        "candles_analysed": len(candles),
        "first_time": candles[0].open_time // 1000,
        "last_time": candles[-1].open_time // 1000,
        "accepted": accepted,
        "rejected": result["rejected"],
        "subwaves": subwaves,
        "pivots": [{"index": p.index, "time": p.timestamp // 1000, "price": p.price,
                    "kind": p.kind} for p in result["pivots"]],
        "coverage": best["coverage"],
        "projection": projection,
        "invalidation": _invalidation_for(developing or (accepted[-1] if accepted else None), degree),
        # Only meaningful when the newest structure is a finished 1-5: it
        # says where the correction that follows one usually reaches.
        "correction_zone": _correction_zone(accepted[-1] if accepted else None),
        "developing_label": (projection or {}).get("next_label"),
        "deviation_search": [{"deviation_pct": a["deviation_pct"], "pivots": a["pivots"],
                              "structures": a["structures"],
                              "covered_fraction": round(a["covered_fraction"], 4),
                              "mean_score": round(a["mean_score"], 3)}
                             for a in attempts],
    }
