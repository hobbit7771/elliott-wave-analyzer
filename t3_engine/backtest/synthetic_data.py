"""SYNTHETIC price fixture generator - NOT real market data.

Why this exists: this build session's sandbox blocks outbound access to
`fapi.binance.com` (see market_data/rest_client.py docstring), so no real
historical klines could be downloaded here to exercise the backtester
end-to-end. Rather than skip demonstrating the backtest pipeline entirely,
this module generates a deterministic, clearly-labelled-as-synthetic OHLCV
series with a known, rule-valid 1-2-3-4-5-A-B-C Elliott structure baked in
(realistic Fibonacci retracements/extensions, small noise), so:
  (a) the full pipeline (pivots -> structure -> scenarios -> signals ->
      risk -> execution -> metrics) can be run and unit-tested honestly,
      and
  (b) the "no lookahead" property of the whole chain can be verified by
      replaying it forward and asserting no future bar ever influences a
      decision made on an earlier one.

THIS IS NOT A SUBSTITUTE for section 19's requirement of real historical
backtesting across 1000+ trades. That must be run by the user against real
Binance history in an environment with normal internet access - point
`run_backtest.py` at `BinanceFuturesREST.get_klines(...)` instead of this
generator to do that.
"""

from __future__ import annotations

import random
from typing import Dict, List

from t3_engine.common.models import Candle
from t3_engine.common.types import Timeframe


def _segment(candles: List[Candle], start_price: float, end_price: float, n_bars: int,
             start_time: int, bar_seconds: int, noise: float, rng: random.Random) -> float:
    step = (end_price - start_price) / n_bars
    price = start_price
    t = start_time
    for i in range(n_bars):
        open_p = price
        close_p = price + step * (1 + rng.uniform(-noise, noise))
        high_p = max(open_p, close_p) * (1 + abs(rng.uniform(0, noise)))
        low_p = min(open_p, close_p) * (1 - abs(rng.uniform(0, noise)))
        volume = 100 * (1 + rng.uniform(-0.2, 0.5))
        taker_buy = volume * (0.5 + (0.15 if close_p >= open_p else -0.15) + rng.uniform(-0.05, 0.05))
        candles.append(Candle(
            timeframe=Timeframe.M5, open_time=t, close_time=t + bar_seconds * 1000 - 1,
            open=open_p, high=high_p, low=low_p, close=close_p,
            volume=volume, taker_buy_volume=max(0.0, min(volume, taker_buy)), trades=50, closed=True,
        ))
        price = close_p
        t += bar_seconds * 1000
    return price


def generate_synthetic_impulse_cycle(start_price: float = 100.0, start_time: int = 0,
                                      bar_seconds: int = 300, noise: float = 0.003,
                                      seed: int = 42) -> List[Candle]:
    """One full, hard-rule-valid impulse (1-2-3-4-5) followed by an ABC
    correction, expressed as 5m candles with plausible Fibonacci internal
    structure. Deterministic given `seed`."""
    rng = random.Random(seed)
    candles: List[Candle] = []
    t = start_time
    p = start_price

    # Wave 1: up 20%
    w1_start = p
    p = _segment(candles, p, p * 1.20, 12, t, bar_seconds, noise, rng); t = candles[-1].close_time + 1
    w1_end = p
    # Wave 2: retrace 50% of wave1
    w2_target = w1_end - (w1_end - w1_start) * 0.5
    p = _segment(candles, p, w2_target, 8, t, bar_seconds, noise, rng); t = candles[-1].close_time + 1
    # Wave 3: extend 1.618x wave1 length from wave2 end
    w3_target = p + (w1_end - w1_start) * 1.618
    p = _segment(candles, p, w3_target, 20, t, bar_seconds, noise, rng); t = candles[-1].close_time + 1
    w3_end = p
    w3_start = w2_target
    # Wave 4: retrace 30% of wave3, staying above wave1 high (no overlap)
    w4_target = max(w3_end - (w3_end - w3_start) * 0.30, w1_end * 1.01)
    p = _segment(candles, p, w4_target, 10, t, bar_seconds, noise, rng); t = candles[-1].close_time + 1
    # Wave 5: equal to wave1 length from wave4 end
    w5_target = p + (w1_end - w1_start) * 1.0
    p = _segment(candles, p, w5_target, 12, t, bar_seconds, noise, rng); t = candles[-1].close_time + 1
    w5_end = p
    # Wave A: retrace 38% of the whole 1-5 move
    wa_target = w5_end - (w5_end - w1_start) * 0.38
    p = _segment(candles, p, wa_target, 10, t, bar_seconds, noise, rng); t = candles[-1].close_time + 1
    # Wave B: retrace 50% of wave A
    wb_target = p + (w5_end - wa_target) * 0.5
    p = _segment(candles, p, wb_target, 7, t, bar_seconds, noise, rng); t = candles[-1].close_time + 1
    # Wave C: 1.0x wave A length
    wc_target = p - (w5_end - wa_target) * 1.0
    p = _segment(candles, p, wc_target, 12, t, bar_seconds, noise, rng)

    return candles


def generate_synthetic_series(num_cycles: int = 3, start_price: float = 100.0, seed: int = 42) -> List[Candle]:
    all_candles: List[Candle] = []
    price = start_price
    t = 0
    for cycle in range(num_cycles):
        cycle_candles = generate_synthetic_impulse_cycle(start_price=price, start_time=t, seed=seed + cycle)
        all_candles.extend(cycle_candles)
        price = cycle_candles[-1].close
        t = cycle_candles[-1].close_time + 1
    return all_candles


# How many 5m bars one synthetic cycle produces. Asserted by a test rather
# than trusted: the aggregation below sizes its request from it.
_CANDLES_PER_CYCLE = 91

# The generator emits 5m bars. Anything coarser is built by AGGREGATING
# them, exactly the way an exchange builds a 4h bar out of its 5m ones -
# not by generating a separate, unrelated series at that timeframe.
_BASE_SECONDS = 300

# A ceiling on how many base bars one request may generate. A 4h series
# needs 48 base bars per output bar, so without this a large `cycles` asks
# for hundreds of thousands of candles to be built and thrown away.
MAX_BASE_CANDLES = 20_000


def base_seconds_of(candles: List[Candle]) -> int:
    """How long one bar of THIS series is, in seconds.

    Taken from the candles themselves rather than assumed. The function
    below used to divide by the module-level `_BASE_SECONDS` (300, because
    the synthetic generator emits 5m bars), which is right only when the
    input really is 5m. Fed a stored 15m series it computed 48 base bars
    per 4h bar, found 16 in every bucket, discarded all of them and
    returned an empty chart - wave labels drawn over no candles.

    The declared timeframe is trusted first, because it is what every
    producer in this codebase sets. The spacing between opens is the
    fallback, for a series assembled without one.
    """
    if not candles:
        return _BASE_SECONDS
    declared = getattr(candles[0].timeframe, "seconds", 0) or 0
    if declared > 0:
        return int(declared)
    gaps = sorted(b.open_time - a.open_time for a, b in zip(candles, candles[1:])
                  if b.open_time > a.open_time)
    if gaps:
        return max(1, gaps[len(gaps) // 2] // 1000)
    return _BASE_SECONDS


def aggregate_candles(candles: List[Candle], timeframe: Timeframe) -> List[Candle]:
    """Roll bars up into a coarser timeframe.

    Plain OHLCV aggregation: first open, highest high, lowest low, last
    close, summed volume. A trailing group that is not yet full is dropped
    rather than emitted short - a half-formed 4h bar presented as a closed
    one is the same lookahead the rest of this engine refuses.

    The input's own bar length decides how many bars a full bucket holds,
    so this works on any series, not only the 5m one the synthetic
    generator produces.
    """
    if not candles:
        return []
    base = base_seconds_of(candles)
    if timeframe.seconds <= base:
        # Same degree, or finer than what we have. Aggregation cannot
        # invent a shorter bar, so the series is returned as it is.
        return list(candles)
    per_bar = timeframe.seconds // base
    if per_bar <= 1:
        return list(candles)

    # Group by ABSOLUTE time bucket, not by position in the list. An
    # exchange's 4h bar starts at 00:00, 04:00, 08:00 UTC - it does not
    # start wherever the data happens to begin. Chunking positionally
    # gives bars that are the right LENGTH at the wrong OFFSET, and a wave
    # labelled on one alignment does not line up with a chart drawn on the
    # other. (Identical to positional chunking for the synthetic fixture,
    # which starts at t=0.)
    bucket_ms = timeframe.seconds * 1000
    groups: Dict[int, List[Candle]] = {}
    for candle in candles:
        groups.setdefault(candle.open_time // bucket_ms, []).append(candle)

    out: List[Candle] = []
    for bucket in sorted(groups):
        group = groups[bucket]
        # A bucket that is not full is a bar still forming (or a gap in the
        # data): emitting it as closed is the lookahead this engine refuses.
        if len(group) < per_bar:
            continue
        out.append(Candle(
            timeframe=timeframe,
            open_time=group[0].open_time,
            close_time=group[-1].close_time,
            open=group[0].open,
            high=max(c.high for c in group),
            low=min(c.low for c in group),
            close=group[-1].close,
            volume=sum(c.volume for c in group),
            taker_buy_volume=sum(c.taker_buy_volume for c in group),
            trades=sum(c.trades for c in group),
        ))
    return out


def _mirror_cycle(candles: List[Candle], pivot: float, scale: float = 1.0) -> List[Candle]:
    """Reflect a bull cycle around `pivot` to get the bear one.

    The map is p -> pivot - scale * (p - pivot): it negates every price
    DIFFERENCE and multiplies them all by the same factor, which leaves the
    RATIOS between them untouched. So the mirrored cycle is still a
    hard-rule-valid impulse - wave 3 still the longest, wave 4 still not
    overlapping wave 1 - pointing down instead of up.

    `scale` is what keeps a long series from drifting. A cycle rises about
    53%, so a plain reflection of the next one falls 53% of the NEW, higher
    base and the pair loses ground; repeated fifty times that walks the
    chart to nearly zero (measured: a 4h series ended at 8.00 having
    started at 152). Passing scale = pair_start / price_now makes the pair
    land exactly back where it began.

    High and low swap, because a reflection turns the top of a bar into its
    bottom."""
    def flip(price: float) -> float:
        return pivot - scale * (price - pivot)

    out: List[Candle] = []
    for candle in candles:
        out.append(Candle(
            timeframe=candle.timeframe, open_time=candle.open_time,
            close_time=candle.close_time,
            open=flip(candle.open), high=flip(candle.low),
            low=flip(candle.high), close=flip(candle.close),
            volume=candle.volume,
            # Taker pressure follows the bar, so it flips with it.
            taker_buy_volume=max(0.0, candle.volume - candle.taker_buy_volume),
            trades=candle.trades, closed=candle.closed,
        ))
    return out


def generate_alternating_series(num_cycles: int = 3, start_price: float = 100.0,
                                seed: int = 42) -> List[Candle]:
    """Base 5m bars whose cycles alternate up, down, up, down.

    `generate_synthetic_series` compounds every cycle off the previous
    close, and one cycle is about +53%. Over the three cycles it was built
    for that is fine; over the ~96 a 4h series needs it reaches 3e10 and
    the "chart" is a vertical line. Alternating the direction keeps the
    same rule-valid structure while the series oscillates instead of
    running away."""
    out: List[Candle] = []
    price = start_price
    pair_start = start_price
    t = 0
    for cycle in range(max(1, num_cycles)):
        block = generate_synthetic_impulse_cycle(start_price=price, start_time=t,
                                                 seed=seed + cycle)
        if cycle % 2 == 0:
            pair_start = price          # the up leg of this pair begins here
        else:
            # Scaled so the down leg returns exactly to where the up leg
            # started - see _mirror_cycle for why a plain reflection drifts.
            block = _mirror_cycle(block, price, scale=pair_start / price if price else 1.0)
        out.extend(block)
        price = block[-1].close
        t = block[-1].close_time + 1
    return out


def generate_synthetic_series_for(timeframe: Timeframe, num_cycles: int = 3,
                                  start_price: float = 100.0, seed: int = 42) -> List[Candle]:
    """A synthetic series AT the requested timeframe.

    This exists because of a real defect: the demo source returned the same
    5m series whatever timeframe was asked for, so a multi-timeframe run
    analysed one chart four times and then "reconciled" it with itself. The
    model noticed before anyone else did - it wrote "the supplied data
    repeats 5m" into its own verdict and refused to call a trend.

    Enough base bars are generated that the aggregated series ends up about
    as long as the 5m one would have been for the same `num_cycles`, so a
    4h request gets a 4h-shaped chart rather than three bars.
    """
    per_bar = max(1, timeframe.seconds // _BASE_SECONDS)
    wanted_base = num_cycles * _CANDLES_PER_CYCLE * per_bar
    cycles = max(1, min(
        -(-wanted_base // _CANDLES_PER_CYCLE),                 # ceiling division
        MAX_BASE_CANDLES // _CANDLES_PER_CYCLE,
    ))
    base = generate_alternating_series(num_cycles=cycles, start_price=start_price, seed=seed)
    return aggregate_candles(base, timeframe)
