"""Five independent readings of the market, scored separately.

The first build produced one weighted sum of nine flat components. That
is worse than it looks: a reading could not be attributed to a source, two
sources could not be seen to disagree, and a strong structure reading plus
a weak-but-opposite flow reading averaged into a mild number that said
nothing about either.

So the market is read five times, by five groups of features that come
from genuinely different places:

    STRUCTURE    where price is, in the shape it has been making
    FLOW         who is crossing the spread, how hard
    BOOK         what is resting, and whether the depths agree
    DERIVATIVES  positioning: open interest, forced closes, funding
    BTC_LEAD     whether the thing that moves everything is moving

Each returns a signed score on -1..+1, a long and a short number on 0..1,
a CONFIDENCE (what share of its own inputs were actually available), and
the full breakdown. Only then are they combined - see pressure_engine.

Confidence is not a fudge factor. A layer scoring +0.8 from one of its
five inputs is a different claim from the same +0.8 with all five, and
without that distinction a freshly-connected engine looks as sure of
itself as a warmed-up one.

Every raw quantity that is not already bounded goes through
`normalize.Normalizer` on the way in, so nothing here compares an
instrument against a constant that was typed for a different instrument.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from t3_engine.lead_engine.normalize import (
    Normalizer,
    agreement,
    clamp,
    normalized_delta,
    signed_strength,
    saturate,
    split_direction,
)

STRUCTURE = "structure"
FLOW = "flow"
BOOK = "book"
DERIVATIVES = "derivatives"
BTC_LEAD = "btc_lead"

LAYERS = (FLOW, BOOK, STRUCTURE, DERIVATIVES, BTC_LEAD)


@dataclass
class LayerScore:
    name: str
    score: float = 0.0
    confidence: float = 0.0
    label: str = ""
    detail: Dict[str, float] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)

    @property
    def long(self) -> float:
        return split_direction(self.score)[0]

    @property
    def short(self) -> float:
        return split_direction(self.score)[1]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "score": round(self.score, 4),
            "long": round(100.0 * self.long, 2),
            "short": round(100.0 * self.short, 2),
            "confidence": round(self.confidence, 3),
            "label": self.label,
            "detail": {k: round(v, 4) for k, v in self.detail.items()},
            "missing": list(self.missing),
        }


def _combine(parts: Dict[str, Optional[float]], weights: Dict[str, float],
             name: str) -> LayerScore:
    """Weighted mean of whatever parts are present.

    Absent parts are dropped and their weight redistributed - the same
    rule the top-level score uses, for the same reason: counting a stream
    that has not started as a neutral zero makes an uninformed engine look
    like a calm one. Confidence is the share of the layer's total weight
    that was actually answered."""
    available = {k: v for k, v in parts.items() if v is not None}
    missing = [k for k, v in parts.items() if v is None]
    total_weight = sum(weights.get(k, 0.0) for k in available)
    all_weight = sum(weights.values())
    if total_weight <= 0:
        return LayerScore(name=name, score=0.0, confidence=0.0, missing=missing)
    score = sum(weights[k] * clamp(v) for k, v in available.items()) / total_weight
    return LayerScore(
        name=name, score=clamp(score),
        confidence=round(total_weight / all_weight, 4) if all_weight > 0 else 0.0,
        detail={k: clamp(v) for k, v in available.items()},
        missing=missing,
    )


def _label(score: float, strong: float = 0.4, weak: float = 0.15) -> str:
    if score >= strong:
        return "BULLISH"
    if score >= weak:
        return "LEAN_BULLISH"
    if score <= -strong:
        return "BEARISH"
    if score <= -weak:
        return "LEAN_BEARISH"
    return "NEUTRAL"


# --------------------------------------------------------------------------
# FLOW
# --------------------------------------------------------------------------

FLOW_WEIGHTS = {
    "normalized_delta_5s": 0.30,
    "normalized_delta_60s": 0.20,
    "cvd_slope": 0.25,
    "large_trade_imbalance": 0.12,
    "velocity_signed": 0.13,
}


def score_flow(flow, cvd, normalizer: Normalizer) -> LayerScore:
    """Who is crossing the spread.

    `normalized_delta` replaces the old buy/sell ratio everywhere it
    mattered: the ratio is unbounded (0.9 bought against nothing sold is
    nine hundred million) and cannot be summed with anything. The ratio
    survives as a diagnostic field on the window and takes no part here."""
    parts: Dict[str, Optional[float]] = {}

    window_5s = flow.flow(5_000)
    window_60s = flow.flow(60_000)
    parts["normalized_delta_5s"] = (
        normalized_delta(window_5s.buy_volume, window_5s.sell_volume)
        if window_5s.trades else None)
    parts["normalized_delta_60s"] = (
        normalized_delta(window_60s.buy_volume, window_60s.sell_volume)
        if window_60s.trades else None)

    # CVD as a normalised SLOPE rather than a level: the level is an
    # arbitrary running total whose size depends on how long the process
    # has been up.
    slope = cvd.cvd_change(60_000)
    # Sign from the slope, magnitude from its own history. A z-score here
    # turned "buying, weaker than it was" into selling - see
    # normalize.signed_strength.
    parts["cvd_slope"] = signed_strength(normalizer, "cvd_slope_60s", slope)

    large = flow.large_trades(60_000)
    large_total = large["buy_volume"] + large["sell_volume"]
    parts["large_trade_imbalance"] = (
        normalized_delta(large["buy_volume"], large["sell_volume"])
        if large_total > 0 else None)

    # Velocity has no direction of its own, so it is signed by the flow it
    # accompanies. Fast trading in a balanced market is noise.
    if len(flow.trades):
        magnitude = saturate(max(0.0, flow.velocity_zscore()), 3.0)
        direction = parts["normalized_delta_5s"] or 0.0
        parts["velocity_signed"] = clamp(magnitude * direction)
    else:
        parts["velocity_signed"] = None

    layer = _combine(parts, FLOW_WEIGHTS, FLOW)
    layer.label = _label(layer.score)
    return layer


# --------------------------------------------------------------------------
# BOOK
# --------------------------------------------------------------------------

BOOK_WEIGHTS = {
    "alignment": 0.34,
    "microprice": 0.18,
    "liquidity_shift": 0.18,
    "wall_bias": 0.15,
    "absorption": 0.15,
}


def absorption_scores(book, normalizer: Normalizer) -> Dict[str, float]:
    """Absorption on 0..1 per side, normalised three ways.

    The raw figure the first build printed - `added / executed`, a number
    like 68815 - is meaningless on its own: it is unbounded, it means
    different things at different activity levels, and it was on screen
    beside numbers living in -1..+1.

    Here it is measured against the volume that actually traded, against
    the depth on that side, and against its own recent distribution, and
    the three are averaged. Absorption is "size replaced as fast as it was
    hit", so all three angles are about the same question and disagreeing
    is informative rather than a problem."""
    raw = book.raw_absorption()
    out: Dict[str, float] = {}
    for side in ("bid", "ask"):
        added = raw[f"{side}_added"]
        executed = raw[f"{side}_executed"]
        depth = raw[f"{side}_depth"]
        if executed <= 0:
            # Nothing traded into this side, so nothing was absorbed.
            # Zero, not "unknown": absorption with no aggression is not a
            # missing reading, it is an absent event.
            out[f"{side}_absorption_score"] = 0.0
            out[f"{side}_absorption_vs_volume"] = 0.0
            out[f"{side}_absorption_vs_depth"] = 0.0
            out[f"{side}_absorption_percentile"] = 0.5
            continue
        vs_volume = saturate(added / executed, 2.0)
        vs_depth = saturate(added / depth, 0.5) if depth > 0 else 0.0
        percentile = normalizer.update(f"{side}_absorption", added / executed,
                                       "percentile")
        out[f"{side}_absorption_vs_volume"] = round(vs_volume, 4)
        out[f"{side}_absorption_vs_depth"] = round(vs_depth, 4)
        out[f"{side}_absorption_percentile"] = round(percentile, 4)
        out[f"{side}_absorption_score"] = round(
            (vs_volume + vs_depth + percentile) / 3.0, 4)
    return out


def score_book(book, microprice, normalizer: Normalizer) -> LayerScore:
    """What is resting, and whether the depths agree about it."""
    parts: Dict[str, Optional[float]] = {}
    detail_extra: Dict[str, float] = {}

    if not book.synced:
        layer = LayerScore(name=BOOK, score=0.0, confidence=0.0,
                           missing=list(BOOK_WEIGHTS), label="NO_BOOK")
        return layer

    alignment = book.alignment()
    parts["alignment"] = alignment["book_alignment"]
    detail_extra.update({
        "top_book_score": alignment["top_book_score"],
        "deep_book_score": alignment["deep_book_score"],
        "book_consistency": alignment["consistency"],
    })

    offset = microprice.offset_bps()
    # Same reason as cvd_slope: the microprice offset is a DIRECTION, and
    # a z-score can invert the sign of one that never changed sign.
    parts["microprice"] = signed_strength(normalizer, "microprice_offset", offset)

    metrics = book.metrics()
    bid_side = metrics.bid_replenishment - metrics.bid_pulling
    ask_side = metrics.ask_replenishment - metrics.ask_pulling
    total = abs(bid_side) + abs(ask_side)
    parts["liquidity_shift"] = ((bid_side - ask_side) / total) if total > 0 else None

    walls = book.wall_summary()
    parts["wall_bias"] = walls["wall_bias"] if walls["walls"] else None
    detail_extra["spoofs_60s"] = float(walls["spoofs_60s"])

    absorption = absorption_scores(book, normalizer)
    bid_abs = absorption["bid_absorption_score"]
    ask_abs = absorption["ask_absorption_score"]
    # Bids absorbing selling is support; asks absorbing buying is supply.
    parts["absorption"] = clamp(bid_abs - ask_abs) if (bid_abs or ask_abs) else None
    detail_extra.update(absorption)

    layer = _combine(parts, BOOK_WEIGHTS, BOOK)
    layer.detail.update(detail_extra)
    layer.label = book.alignment_label()
    return layer


# --------------------------------------------------------------------------
# STRUCTURE
# --------------------------------------------------------------------------

STRUCTURE_WEIGHTS = {
    "trend": 0.28,
    "break": 0.30,
    "sweep": 0.14,
    "elliott": 0.16,
    "invalidation_distance": 0.12,
}


def score_structure(smc_state, elliott_component: Optional[float],
                    price: Optional[float]) -> LayerScore:
    """Where price is in the shape it has been making."""
    parts: Dict[str, Optional[float]] = {}
    if not smc_state:
        return LayerScore(name=STRUCTURE, score=0.0, confidence=0.0,
                          missing=list(STRUCTURE_WEIGHTS), label="NO_STRUCTURE")

    trend = smc_state.get("trend")
    parts["trend"] = 0.6 if trend == "bullish" else -0.6 if trend == "bearish" else 0.0

    # A change of character outranks a break with the trend: the first
    # sign a trend is over is worth more than its continuation.
    if smc_state.get("choch"):
        parts["break"] = 1.0 if smc_state.get("choch_direction") == "bullish" else -1.0
    elif smc_state.get("bos"):
        parts["break"] = 0.5 if smc_state.get("bos_direction") == "bullish" else -0.5
    else:
        parts["break"] = 0.0

    sweep = smc_state.get("sweep")
    parts["sweep"] = 0.7 if sweep == "low" else -0.7 if sweep == "high" else 0.0

    parts["elliott"] = elliott_component

    # How far price sits from the level that would invalidate the current
    # reading. Close to it means the structure is fragile whichever way it
    # points, so this term pulls TOWARD zero rather than picking a side.
    high, low = smc_state.get("swing_high"), smc_state.get("swing_low")
    if price and high and low and high > low:
        position = (price - low) / (high - low)
        # Deep in the range is comfortable; at either edge the count is
        # one candle from being wrong.
        parts["invalidation_distance"] = clamp(2.0 * (0.5 - abs(position - 0.5)) - 0.5)
    else:
        parts["invalidation_distance"] = None

    layer = _combine(parts, STRUCTURE_WEIGHTS, STRUCTURE)
    layer.label = _label(layer.score)
    if smc_state.get("choch"):
        layer.label = f"CHOCH_{str(smc_state.get('choch_direction', '')).upper()}"
    elif smc_state.get("bos"):
        layer.label = f"BOS_{str(smc_state.get('bos_direction', '')).upper()}"
    return layer


# --------------------------------------------------------------------------
# DERIVATIVES
# --------------------------------------------------------------------------

DERIVATIVES_WEIGHTS = {
    "open_interest": 0.40,
    "liquidations": 0.40,
    "funding": 0.20,
}


def score_derivatives(open_interest, liquidations, ticker: Dict[str, Any],
                      normalizer: Normalizer) -> LayerScore:
    """Positioning: who is opening, who is being closed for them."""
    parts: Dict[str, Optional[float]] = {}
    detail_extra: Dict[str, float] = {}

    parts["open_interest"] = (open_interest.pressure_component()
                              if len(open_interest.series) >= 2 else None)

    if len(liquidations.events):
        parts["liquidations"] = liquidations.pressure_component()
        velocity = liquidations.velocity(5_000)
        detail_extra["liquidation_velocity_percentile"] = normalizer.update(
            "liquidation_velocity", velocity, "percentile")
    else:
        parts["liquidations"] = None

    # Funding, when the ticker carries it. A high positive rate means
    # longs are paying to stay - crowded, and a headwind - so it scores
    # AGAINST the crowd rather than with it.
    funding_raw = ticker.get("fundingRate") if ticker else None
    try:
        funding = float(funding_raw) if funding_raw is not None else None
    except (TypeError, ValueError):
        funding = None
    if funding is not None:
        # Negated because positive funding means longs are paying, which
        # is crowding on the long side and therefore a bearish lean. The
        # sign still comes from the rate itself, not from a z-score.
        normalised = signed_strength(normalizer, "funding", funding)
        parts["funding"] = clamp(-normalised) if normalised is not None else None
        detail_extra["funding_rate"] = funding
    else:
        parts["funding"] = None

    layer = _combine(parts, DERIVATIVES_WEIGHTS, DERIVATIVES)
    layer.detail.update(detail_extra)
    layer.label = str(liquidations.state()) if len(liquidations.events) else _label(layer.score)
    return layer


# --------------------------------------------------------------------------
# BTC LEAD
# --------------------------------------------------------------------------

BTC_WEIGHTS = {
    "lead_score": 0.55,
    "btc_flow": 0.25,
    "btc_book": 0.20,
}


def score_btc_lead(lead_state: Dict[str, Any], btc_flow_score: Optional[float],
                   btc_book_score: Optional[float], is_btc: bool) -> LayerScore:
    """Whether the instrument that moves everything is moving.

    For BTCUSDT itself this layer is deliberately empty: BTC does not lead
    itself, and giving it a self-correlation of 1.0 would be true and
    useless. Its weight is redistributed over the other four."""
    if is_btc:
        return LayerScore(name=BTC_LEAD, score=0.0, confidence=0.0,
                          missing=list(BTC_WEIGHTS), label="SELF")

    parts: Dict[str, Optional[float]] = {
        "lead_score": lead_state.get("btc_lead_score"),
        # BTC's own flow and book, carried across in proportion to how
        # related the two instruments actually are.
        "btc_flow": btc_flow_score,
        "btc_book": btc_book_score,
    }
    correlation = lead_state.get("correlation")
    if correlation is not None:
        carry = abs(float(correlation))
        sign = 1.0 if float(correlation) >= 0 else -1.0
        for key in ("btc_flow", "btc_book"):
            if parts[key] is not None:
                parts[key] = clamp(float(parts[key]) * carry * sign)
    else:
        parts["btc_flow"] = None
        parts["btc_book"] = None

    layer = _combine(parts, BTC_WEIGHTS, BTC_LEAD)
    layer.label = str(lead_state.get("btc_lead_state") or _label(layer.score))
    return layer


# --------------------------------------------------------------------------
# conflict
# --------------------------------------------------------------------------

CONFLICT_HIGH = "CONFLICT_HIGH"
CONFLICT_MEDIUM = "CONFLICT_MEDIUM"
CONFLICT_LOW = "CONFLICT_LOW"

# Agreement below this is a high conflict, and above the second a low one.
HIGH_CONFLICT_AGREEMENT = 0.40
LOW_CONFLICT_AGREEMENT = 0.75


@dataclass
class Conflict:
    level: str = CONFLICT_LOW
    agreement: float = 1.0
    penalty: float = 1.0
    opposing: List[str] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"level": self.level, "agreement": round(self.agreement, 4),
                "confidence_multiplier": round(self.penalty, 4),
                "opposing_layers": list(self.opposing), "note": self.note}


def detect_conflict(layers: Dict[str, LayerScore]) -> Conflict:
    """How much the five independent readings disagree.

    The old `conflict` was `min(long_pressure, short_pressure)` - it
    noticed that both sides had scored, but not WHICH sources disagreed.
    Structure bullish against flow and book bearish produced the same
    number as nine mildly mixed components, which are not the same
    situation at all: the first is a real argument between independent
    observers and the second is noise.

    Here the disagreement is measured across LAYERS, weighted by how
    strongly each is speaking, and the result multiplies confidence
    rather than the score. A contested market still reports what each
    layer sees; it just stops claiming to be sure."""
    live = {name: layer for name, layer in layers.items()
            if layer.confidence > 0 and abs(layer.score) > 1e-9}
    if len(live) < 2:
        return Conflict(level=CONFLICT_LOW, agreement=1.0, penalty=1.0,
                        note="too few layers speaking to disagree")

    scores = [layer.score for layer in live.values()]
    unity = agreement(scores)
    net = sum(scores)
    direction = 1.0 if net >= 0 else -1.0
    opposing = sorted(name for name, layer in live.items()
                      if layer.score * direction < 0)

    if unity < HIGH_CONFLICT_AGREEMENT:
        level, penalty = CONFLICT_HIGH, 0.35
    elif unity < LOW_CONFLICT_AGREEMENT:
        level, penalty = CONFLICT_MEDIUM, 0.7
    else:
        level, penalty = CONFLICT_LOW, 1.0

    note = ""
    if opposing:
        leading = "long" if direction > 0 else "short"
        note = (f"{', '.join(opposing)} disagree with the {leading} reading "
                f"of the others")
    return Conflict(level=level, agreement=unity, penalty=penalty,
                    opposing=opposing, note=note)
