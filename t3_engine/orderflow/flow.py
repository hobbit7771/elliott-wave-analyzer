"""Taker order-flow analysis on 1s-3m candles (spec section 3: these
timeframes may only CONFIRM, never originate, a trade)."""

from __future__ import annotations

from typing import List, Optional

from t3_engine.common.models import Candle
from t3_engine.common.types import Direction


def taker_buy_ratio(candle: Candle) -> float:
    if candle.volume == 0:
        return 0.5
    return candle.taker_buy_volume / candle.volume


def taker_flow_supports_direction(candles: List[Candle], direction: Direction, lookback: int = 5, threshold: float = 0.55) -> bool:
    """True if, over the last `lookback` closed candles, the average taker
    buy ratio confirms the given direction."""
    recent = candles[-lookback:]
    if not recent:
        return False
    avg_ratio = sum(taker_buy_ratio(c) for c in recent) / len(recent)
    return avg_ratio >= threshold if direction == Direction.UP else avg_ratio <= (1 - threshold)


def detect_absorption(candles: List[Candle], lookback: int = 5) -> Optional[Direction]:
    """Absorption: heavy volume with a small resulting range and price
    failing to make further progress - a sign big passive orders are
    soaking up aggression at a level (classic reversal/continuation tell
    used to confirm a wave termination zone)."""
    recent = candles[-lookback:]
    if len(recent) < 2:
        return None
    avg_volume = sum(c.volume for c in recent) / len(recent)
    last = recent[-1]
    if last.volume <= avg_volume * 1.5:
        return None
    if last.range == 0:
        return None
    body_frac = abs(last.close - last.open) / last.range
    if body_frac > 0.35:
        return None  # not absorption, price actually moved cleanly
    # direction of the absorption = direction of the dominant taker side
    return Direction.UP if taker_buy_ratio(last) > 0.5 else Direction.DOWN


def detect_divergence(candles: List[Candle], price_key: str = "close", lookback: int = 10) -> Optional[str]:
    """Very small, causal divergence check: compares the two most recent
    swing extremes in price vs. taker buy ratio as a momentum proxy.
    Returns 'BULLISH', 'BEARISH' or None. This is intentionally a coarse
    heuristic (a full RSI/MACD divergence detector lives in momentum.py
    and should be combined with this by the signal engine)."""
    recent = candles[-lookback:]
    if len(recent) < 4:
        return None
    mid = len(recent) // 2
    first_half, second_half = recent[:mid], recent[mid:]
    price_first = max(getattr(c, price_key) for c in first_half)
    price_second = max(getattr(c, price_key) for c in second_half)
    flow_first = sum(taker_buy_ratio(c) for c in first_half) / len(first_half)
    flow_second = sum(taker_buy_ratio(c) for c in second_half) / len(second_half)

    if price_second > price_first and flow_second < flow_first:
        return "BEARISH"  # higher price, weaker buying pressure

    price_first_low = min(c.low for c in first_half)
    price_second_low = min(c.low for c in second_half)
    if price_second_low < price_first_low and flow_second > flow_first:
        return "BULLISH"  # lower price, stronger buying pressure

    return None
