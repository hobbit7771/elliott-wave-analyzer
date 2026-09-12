from t3_engine.common.models import Candle
from t3_engine.common.types import Timeframe, Direction
from t3_engine.market_structure.pivots import ZigZagPivotDetector
from t3_engine.market_structure.structure import MarketStructureTracker


def candle(i, o, h, l, c):
    return Candle(Timeframe.M5, i * 300000, i * 300000 + 299999, o, h, l, c, volume=1, closed=True)


def feed(prices, deviation=1.0):
    """prices: list of (open, high, low, close). Returns list of confirmed pivots."""
    det = ZigZagPivotDetector(deviation_pct=deviation)
    pivots = []
    for i, (o, h, l, c) in enumerate(prices):
        p = det.update(i, candle(i, o, h, l, c))
        if p:
            pivots.append(p)
    return pivots, det


def test_zigzag_detects_alternating_swings_no_lookahead():
    # up to 110, down to 90, up to 120 - with 1% deviation these should register
    prices = [
        (100, 100, 100, 100),
        (100, 110, 100, 110),
        (110, 110, 90, 90),
        (90, 120, 90, 120),
    ]
    pivots, det = feed(prices, deviation=1.0)
    kinds = [p.kind for p in pivots]
    assert "HIGH" in kinds
    assert "LOW" in kinds
    # confirmation must happen strictly after (or same index as) the extreme itself
    for p in pivots:
        assert p.confirmed_at_index >= p.index


def test_zigzag_ignores_moves_below_deviation_threshold():
    prices = [
        (100, 100, 100, 100),
        (100, 100.2, 99.9, 100.1),  # tiny noise, well under 1%
        (100.1, 100.3, 100.0, 100.2),
    ]
    pivots, det = feed(prices, deviation=1.0)
    assert pivots == []


def test_bos_on_continuation_higher_high():
    tracker = MarketStructureTracker()
    from t3_engine.common.models import Pivot
    p1 = Pivot(0, 0, 100, "LOW", 0)
    p2 = Pivot(1, 1, 110, "HIGH", 1)
    p3 = Pivot(2, 2, 105, "LOW", 2)
    p4 = Pivot(3, 3, 115, "HIGH", 3)  # higher high than p2 -> BOS up
    tracker.on_pivot(p1)
    tracker.on_pivot(p2)
    tracker.on_pivot(p3)
    event = tracker.on_pivot(p4)
    assert event is not None
    assert event.kind == "BOS"
    assert event.direction == Direction.UP


def test_choch_on_trend_reversal():
    from t3_engine.common.models import Pivot
    tracker = MarketStructureTracker()
    # establish downtrend: LH then LL then LH then LL
    tracker.on_pivot(Pivot(0, 0, 120, "HIGH", 0))
    tracker.on_pivot(Pivot(1, 1, 100, "LOW", 1))
    tracker.on_pivot(Pivot(2, 2, 110, "HIGH", 2))  # lower high -> BOS down
    tracker.on_pivot(Pivot(3, 3, 90, "LOW", 3))    # lower low -> BOS down, trend=DOWN
    # now price makes a HIGHER high than last swing high (110) -> CHoCH up
    event = tracker.on_pivot(Pivot(4, 4, 125, "HIGH", 4))
    assert event.kind == "CHoCH"
    assert event.direction == Direction.UP


def test_liquidity_sweep_detects_wick_and_reclaim():
    tracker = MarketStructureTracker()
    from t3_engine.common.models import Pivot
    tracker.on_pivot(Pivot(0, 0, 100, "HIGH", 0))
    sweep_candle = candle(1, 98, 101, 97, 99)  # wicks above 100 but closes back below
    sweep = tracker.check_liquidity_sweep(1, sweep_candle)
    assert sweep is not None
    assert sweep.direction == Direction.UP
