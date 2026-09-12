from t3_engine.candle_builder.aggregator import MultiTimeframeCandleBuilder, Trade, TimeframeAggregator, resample_candles
from t3_engine.common.types import Timeframe


def make_trade(ts, price, qty=1.0, buyer_maker=False):
    return Trade(timestamp=ts, price=price, quantity=qty, is_buyer_maker=buyer_maker)


def test_single_bucket_ohlcv():
    agg = TimeframeAggregator(Timeframe.S1)
    agg.on_trade(make_trade(1000, 100))
    agg.on_trade(make_trade(1200, 105))
    agg.on_trade(make_trade(1800, 95))
    assert agg.current_candle.open == 100
    assert agg.current_candle.high == 105
    assert agg.current_candle.low == 95
    assert agg.current_candle.close == 95
    assert agg.current_candle.closed is False


def test_bucket_rollover_closes_previous_candle():
    agg = TimeframeAggregator(Timeframe.S1)
    agg.on_trade(make_trade(1000, 100))
    closed = agg.on_trade(make_trade(2500, 110))
    assert closed is not None
    assert closed.closed is True
    assert closed.close == 100
    assert agg.current_candle.open == 110
    assert len(agg.closed_candles) == 1


def test_taker_buy_volume_tracked_by_convention():
    agg = TimeframeAggregator(Timeframe.S1)
    agg.on_trade(make_trade(1000, 100, qty=2.0, buyer_maker=False))  # taker bought
    agg.on_trade(make_trade(1000, 100, qty=3.0, buyer_maker=True))  # taker sold
    c = agg.current_candle
    assert c.volume == 5.0
    assert c.taker_buy_volume == 2.0
    assert c.taker_sell_volume == 3.0


def test_flush_closes_illiquid_bucket_without_new_trade():
    agg = TimeframeAggregator(Timeframe.S1)
    agg.on_trade(make_trade(1000, 100))
    assert agg.flush(1500) is None  # still inside the same 1s bucket
    closed = agg.flush(3000)
    assert closed is not None
    assert closed.close == 100


def test_no_lookahead_current_candle_excluded_until_closed():
    """A consumer reading `closed_candles` must never see a candle whose
    bucket has not actually finished - that would leak future price info
    into any pivot/structure logic reading the list."""
    agg = TimeframeAggregator(Timeframe.S1)
    agg.on_trade(make_trade(1000, 100))
    agg.on_trade(make_trade(1900, 999))  # extreme price, still same bucket
    assert agg.closed_candles == []  # not leaked
    agg.on_trade(make_trade(2000, 50))  # rolls bucket
    assert len(agg.closed_candles) == 1
    assert agg.closed_candles[0].high == 999  # only now revealed


def test_multi_timeframe_fanout():
    seen = []
    builder = MultiTimeframeCandleBuilder([Timeframe.S1, Timeframe.S5], on_closed_candle=lambda tf, c: seen.append((tf, c)))
    for i in range(12):
        builder.on_trade(make_trade(i * 1000, 100 + i))
    assert any(tf == Timeframe.S1 for tf, _ in seen)
    assert any(tf == Timeframe.S5 for tf, _ in seen)


def test_resample_candles_causal_bucketing():
    from t3_engine.common.models import Candle
    src = [
        Candle(Timeframe.M1, 0, 59999, 1, 2, 0.5, 1.5, volume=1),
        Candle(Timeframe.M1, 60000, 119999, 1.5, 3, 1, 2, volume=1),
        Candle(Timeframe.M1, 120000, 179999, 2, 2.5, 1.8, 2.2, volume=1),
        Candle(Timeframe.M1, 180000, 239999, 2.2, 4, 2, 3.9, volume=1),
        Candle(Timeframe.M1, 240000, 299999, 3.9, 4.1, 3.5, 4, volume=1),
    ]
    out = resample_candles(src, Timeframe.M5)
    assert len(out) == 1
    bar = out[0]
    assert bar.open == 1
    assert bar.close == 4
    assert bar.high == 4.1
    assert bar.low == 0.5
    assert bar.volume == 5
