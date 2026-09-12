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
from typing import List

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
