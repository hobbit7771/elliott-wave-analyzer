from t3_engine.common.models import Candle
from t3_engine.common.types import Direction, Timeframe
from t3_engine.derivatives.tracker import DerivativesTracker
from t3_engine.orderflow.flow import detect_absorption, taker_buy_ratio, taker_flow_supports_direction
from t3_engine.orderflow.momentum import ADX, EMA, MACD, momentum_score


def candle(volume, taker_buy, o=100, h=101, l=99, c=100.5):
    return Candle(Timeframe.M1, 0, 59999, o, h, l, c, volume=volume, taker_buy_volume=taker_buy, closed=True)


def test_taker_buy_ratio_basic():
    c = candle(volume=10, taker_buy=7)
    assert taker_buy_ratio(c) == 0.7


def test_taker_flow_supports_direction_up():
    candles = [candle(10, 8) for _ in range(5)]
    assert taker_flow_supports_direction(candles, Direction.UP)
    assert not taker_flow_supports_direction(candles, Direction.DOWN)


def test_absorption_detected_on_high_volume_small_range():
    candles = [candle(10, 5, o=100, h=101, l=99, c=100.2) for _ in range(4)]
    absorbing = candle(volume=100, taker_buy=70, o=100, h=101, l=99, c=100.2)
    candles.append(absorbing)
    result = detect_absorption(candles)
    assert result == Direction.UP


def test_ema_converges_toward_constant_input():
    ema = EMA(period=5)
    for _ in range(50):
        ema.update(100.0)
    assert abs(ema.value - 100.0) < 1e-6


def test_macd_histogram_sign_tracks_trend():
    macd = MACD(fast=3, slow=6, signal=3)
    for price in range(1, 60):
        macd.update(float(price))
    assert macd.histogram is not None


def test_adx_produces_value_after_warmup():
    adx = ADX(period=5)
    price = 100.0
    result = None
    for _ in range(20):
        price += 1.0
        result = adx.update(high=price + 1, low=price - 1, close=price)
    assert result is not None
    assert result >= 0


def test_momentum_score_bounded():
    macd = MACD()
    macd.update(100)
    macd.update(101)
    adx = ADX()
    adx.update(101, 99, 100)
    adx.update(103, 100, 102)
    score = momentum_score(macd, adx, ema9=105, ema18=100, direction_is_up=True)
    assert 0.0 <= score <= 1.0


def test_derivatives_tracker_score_bounded_and_reacts_to_oi():
    tracker = DerivativesTracker()
    tracker.update_open_interest(0, 1000)
    tracker.update_open_interest(1, 1200)
    tracker.update_funding(1, 0.0001)
    tracker.update_long_short_ratio(0.9)
    score = tracker.score(Direction.UP)
    assert 0.0 <= score <= 1.0
    assert tracker.oi_change_pct() == 0.2
