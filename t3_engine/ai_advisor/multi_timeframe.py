"""Four timeframes, one opinion - and no timeframe recomputed twice.

Elliott analysis is inherently multi-scale: a five-wave advance on 5m can
be wave 1 of a correction on 4h, and a count from one timeframe alone is an
answer to a question nobody asked. This runs 5m, 15m, 1h and 4h, then makes
ONE synthesis call that has to reconcile them.

Three rules shape the design.

CACHE ON DATA, NOT ON A CLOCK. A full analyst run is a dozen model calls
over a whole history; four of them per request would pay for the same
conclusions every time. A saved analysis stays valid until a candle arrives
that it never saw, which for 4h means hours. Only the timeframes that
actually moved are recomputed, and the result says which - the saving is
visible rather than claimed.

THE SYNTHESIS SEES SUMMARIES, NOT HISTORIES. It receives each timeframe's
structures, projection and coverage - a few hundred tokens - never the
candles or pivots again. The per-timeframe work already happened; asking
the model to re-derive it would be paying twice in the same request.

PERCENTAGES ARE MEASURED, NOT ASKED FOR. Target probabilities come from
target_odds.py, which counts how often this chart's own swings reached that
far. A model asked for a percentage returns a confident number with nothing
behind it, and a percentage reads as measurement even when it is invention.
The model may reason about those numbers; it never produces them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import httpx

from t3_engine.ai_advisor import analysis_store, trade_journal
from t3_engine.database import candle_store
from t3_engine.ai_advisor.advisor import (
    ANALYSIS_TEMPERATURE,
    DEFAULT_MODEL,
    DEFAULT_READ_TIMEOUT,
    DEFAULT_THINKING,
    AIAdvisorError,
    _extract_text,
    _parse_count_json,
    _post,
    apply_model_options,
)
from t3_engine.ai_advisor.analyst import DEFAULT_MAX_STEPS, run_analyst
from t3_engine.ai_advisor.target_odds import annotate_projection
from t3_engine.common.types import Timeframe

MTF_TIMEFRAMES = (Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.H4)

SYNTHESIS_SYSTEM_PROMPT = """
You are an Elliott Wave analyst writing one opinion from four timeframes that
have already been counted: 5m, 15m, 1h and 4h. Each summary below carries the
structures that PASSED this server's hard-rule validation, what the count
expects next, and how much of that chart was labelled.

Your job is reconciliation, not re-counting. The higher timeframe sets the
context: a five-wave advance on 5m inside a 4h correction is a bounce, and
saying so is the whole value of looking at four charts instead of one.

Rules for your answer:
- Say plainly where the timeframes AGREE and where they CONFLICT. A conflict
  is information; hiding it to produce a tidy verdict is not.
- The probabilities in the data are MEASURED base rates from each chart's own
  past swings, with sample sizes. You may reason about them. Do not invent
  new percentages of your own, and do not restate these as if they were your
  forecast - say what they are.
- Name what would invalidate your read, as a price.
- If the timeframes genuinely disagree and no honest single read exists, say
  that. "No clear trend" is a valid conclusion and far more useful than a
  confident one that is wrong.

Answer with JSON only, in this shape:
{
  "trend": "UP" | "DOWN" | "MIXED",
  "conviction": "high" | "medium" | "low",
  "headline": "one sentence a trader can act on",
  "expected_move": "where price should go next and via what path",
  "agreement": ["timeframes that support this read, and why"],
  "conflicts": ["timeframes that argue against it, and why"],
  "invalidation": "the price that would end this read",
  "technical_analysis": "several paragraphs: structure by timeframe, the Fibonacci confluence, the volume/momentum picture where the counts imply it, and the levels that matter"
}
""".strip()


@dataclass
class TimeframeAnalysis:
    timeframe: str
    reused: bool
    candles: int
    last_candle_time: int
    accepted: List[Dict[str, Any]] = field(default_factory=list)
    rejected: List[Dict[str, Any]] = field(default_factory=list)
    projection: Optional[Dict[str, Any]] = None
    coverage: Dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    reasoning: str = ""
    steps_used: int = 0
    error: str = ""

    def as_payload(self) -> Dict[str, Any]:
        return {
            "timeframe": self.timeframe, "accepted": self.accepted, "rejected": self.rejected,
            "projection": self.projection, "coverage": self.coverage,
            "summary": self.summary, "reasoning": self.reasoning,
            "steps_used": self.steps_used, "error": self.error,
        }

    def for_synthesis(self) -> Dict[str, Any]:
        """What the synthesis call sees: the conclusions, never the data
        they were derived from."""
        return {
            "timeframe": self.timeframe,
            "chart_labelled_pct": round((self.coverage or {}).get("covered_fraction", 0) * 100),
            "structures": [
                {"kind": s.get("structure"),
                 "waves": [w.get("label") for w in s.get("waves") or []],
                 "from_price": (s.get("waves") or [{}])[0].get("start_price"),
                 "to_price": (s.get("waves") or [{}])[-1].get("end_price"),
                 "note": s.get("note", "")}
                for s in self.accepted
            ],
            "expects_next": self.projection,
            "analyst_summary": self.summary,
        }


@dataclass
class MultiTimeframeResult:
    symbol: str
    per_timeframe: List[TimeframeAnalysis] = field(default_factory=list)
    verdict: Dict[str, Any] = field(default_factory=dict)
    reused_timeframes: List[str] = field(default_factory=list)
    recomputed_timeframes: List[str] = field(default_factory=list)
    model: str = ""
    note: str = ""


def _analyse_timeframe(api_key: str, load_candles: Callable, source: str, symbol: str,
                       timeframe: Timeframe, limit: int, cycles: int, model: str,
                       force: bool, database_url: Optional[str],
                       on_progress: Optional[Callable[[str], None]] = None,
                       **analyst_kwargs) -> TimeframeAnalysis:
    def report(text: str) -> None:
        if on_progress is not None:
            on_progress(text)

    report(f"{timeframe.value}: loading candles.")
    candles, resolved_symbol, degree = load_candles(source, symbol, timeframe.value, limit, cycles)
    if not candles:
        return TimeframeAnalysis(timeframe=timeframe.value, reused=False, candles=0,
                                 last_candle_time=0, error="No candles for this timeframe.")
    newest = candles[-1].open_time
    # The chart this timeframe's count is about, kept with it. See
    # database/candle_store.py - a saved count whose candles are gone can
    # only be re-checked against a re-fetched window, which is a different
    # window and therefore a different claim.
    candle_store.save(resolved_symbol, degree.value, candles, database_url=database_url)

    if not force:
        cached = analysis_store.load(source, resolved_symbol, degree.value, database_url)
        if cached and cached.is_fresh_for(newest):
            payload = cached.payload
            report(f"{degree.value}: reused the saved count - no new candles since it ran.")
            return TimeframeAnalysis(
                timeframe=degree.value, reused=True, candles=cached.candle_count,
                last_candle_time=cached.last_candle_time,
                accepted=payload.get("accepted", []), rejected=payload.get("rejected", []),
                projection=payload.get("projection"), coverage=payload.get("coverage", {}),
                summary=payload.get("summary", ""), reasoning=payload.get("reasoning", ""),
                steps_used=payload.get("steps_used", 0),
            )

    record = trade_journal.summary_for(source, resolved_symbol, degree.value,
                                       database_url=database_url)
    try:
        result = run_analyst(api_key, candles, degree, symbol=resolved_symbol, model=model,
                             on_progress=on_progress,
                             record_line=trade_journal.brief_line(record, resolved_symbol,
                                                                  degree.value),
                             **analyst_kwargs)
    except AIAdvisorError as exc:
        report(f"{degree.value}: failed - {exc}")
        return TimeframeAnalysis(timeframe=degree.value, reused=False, candles=len(candles),
                                 last_candle_time=newest, error=str(exc))

    analysis = TimeframeAnalysis(
        timeframe=degree.value, reused=False, candles=len(candles), last_candle_time=newest,
        accepted=result.accepted, rejected=result.rejected,
        projection=annotate_projection(result.projection, candles),
        coverage=result.coverage, summary=result.summary, reasoning=result.reasoning,
        steps_used=result.steps_used, error=result.error,
    )
    # Only a run that produced something is worth keeping: caching an empty
    # result would suppress the retry that might have worked.
    if analysis.accepted:
        analysis_store.save(source, resolved_symbol, degree.value, newest, len(candles),
                            analysis.as_payload(), model=model, database_url=database_url)
        report(f"{degree.value}: {len(analysis.accepted)} structure(s) in "
               f"{analysis.steps_used} step(s) - saved.")
    else:
        report(f"{degree.value}: no validated structure. {analysis.error}".strip())
    return analysis


def synthesise(api_key: str, analyses: List[TimeframeAnalysis], symbol: str,
               model: str = DEFAULT_MODEL, client: Optional[httpx.Client] = None,
               timeout: float = DEFAULT_READ_TIMEOUT, base_url: Optional[str] = None,
               thinking: Optional[bool] = DEFAULT_THINKING) -> Dict[str, Any]:
    """One call, over summaries. Raises nothing on a malformed answer - a
    verdict that failed to parse comes back as a note, because the
    per-timeframe work is still worth showing."""
    usable = [a for a in analyses if a.accepted]
    if not usable:
        return {"trend": "MIXED", "conviction": "low",
                "headline": "No timeframe produced a validated count, so there is nothing to reconcile.",
                "technical_analysis": ""}

    payload = apply_model_options({
        "messages": [
            {"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
            {"role": "user", "content": f"Instrument: {symbol}\n\n" + json.dumps(
                [a.for_synthesis() for a in usable], separators=(",", ":"), default=str)},
        ],
        "temperature": ANALYSIS_TEMPERATURE,
        "max_tokens": 8192,
        "response_format": {"type": "json_object"},
    }, seed=None, thinking=thinking)

    data = _post(api_key, model, payload, client, timeout, base_url)
    try:
        return _parse_count_json(_extract_text(data))
    except AIAdvisorError as exc:
        return {"trend": "MIXED", "conviction": "low",
                "headline": "The synthesis answer could not be read.",
                "technical_analysis": "", "error": str(exc)}


def run_multi_timeframe(api_key: str, load_candles: Callable, source: str, symbol: str,
                        limit: int = 1500, cycles: int = 2, model: str = DEFAULT_MODEL,
                        timeframes=MTF_TIMEFRAMES, force: bool = False,
                        database_url: Optional[str] = None,
                        client: Optional[httpx.Client] = None,
                        max_steps: int = DEFAULT_MAX_STEPS,
                        on_progress: Optional[Callable[[str], None]] = None,
                        **analyst_kwargs) -> MultiTimeframeResult:
    analyses: List[TimeframeAnalysis] = []
    resolved_symbol = symbol
    for position, timeframe in enumerate(timeframes, start=1):
        if on_progress is not None:
            on_progress(f"[{position}/{len(timeframes)}] {timeframe.value}")
        analysis = _analyse_timeframe(api_key, load_candles, source, symbol, timeframe,
                                      limit, cycles, model, force, database_url,
                                      on_progress=on_progress,
                                      max_steps=max_steps, client=client, **analyst_kwargs)
        analyses.append(analysis)

    reused = [a.timeframe for a in analyses if a.reused]
    recomputed = [a.timeframe for a in analyses if not a.reused and not a.error]
    if on_progress is not None:
        on_progress(f"Reconciling {len(analyses)} timeframes into one verdict.")
    verdict = synthesise(api_key, analyses, resolved_symbol, model=model, client=client,
                         **{k: v for k, v in analyst_kwargs.items()
                            if k in ("timeout", "base_url", "thinking")})

    note = ""
    if reused:
        note = (f"Reused the saved analysis for {', '.join(reused)} - no new candles since it ran. "
                f"Recomputed {', '.join(recomputed) or 'nothing'}.")
    return MultiTimeframeResult(symbol=resolved_symbol, per_timeframe=analyses, verdict=verdict,
                                reused_timeframes=reused, recomputed_timeframes=recomputed,
                                model=model, note=note)
