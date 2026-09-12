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


def test_zigzag_does_not_self_confirm_on_a_single_wide_range_candle():
    """Real bug found in an independent audit: OHLC alone never reveals
    whether a candle's high or low happened first within the bar, so a
    single wide-range trending candle used to both SET a new extreme and
    immediately CONFIRM a pivot against that same just-set extreme (its own
    opposite wick trivially clears any deviation threshold). Once tracking
    a HIGH, a candle whose high extends the extreme must never also
    confirm on that same candle - confirmation requires a LATER candle."""
    # First two candles unambiguously resolve bootstrap into tracking_type
    # "HIGH" via a clean LOW confirmation, with no self-confirmation
    # ambiguity of their own (each only extends one side).
    prices = [
        (100, 100, 100, 100),
        (100, 105, 100, 105),
    ]
    pivots, det = feed(prices, deviation=1.0)
    assert det.tracking_type == "HIGH"
    assert det._extreme.price == 105

    # Now: a single wide-range candle that both makes a NEW high (130) and
    # has a low (95) 27% below it - old code would confirm a HIGH pivot
    # right here, on this same candle, since it never distinguishes "this
    # candle's low retraced from a PRIOR extreme" from "this candle's own
    # high set the extreme its own low is now being compared against".
    p = det.update(2, candle(2, 105, 130, 95, 110))
    assert p is None
    assert det.tracking_type == "HIGH"
    assert det._extreme.price == 130

    # A LATER candle that does NOT extend the high, but reverses far
    # enough from it, is the legitimate confirmation.
    p = det.update(3, candle(3, 110, 115, 100, 105))
    assert p is not None
    assert p.kind == "HIGH"
    assert p.price == 130
    assert p.index == 2  # credited to the candle that made the extreme
    assert p.confirmed_at_index == 3  # but confirmed by a STRICTLY LATER candle
    assert p.confirmed_at_index > p.index


def test_real_pivot_count_drops_sharply_after_self_confirmation_fix():
    """Quantifies the fix's real-world impact rather than just asserting a
    single synthetic case: on the project's own 3-cycle synthetic fixture
    (273 candles), the self-confirmation bug used to produce 267 pivots
    (nearly one per candle - confirmed independently before this fix
    landed). A sane detector should produce far fewer, genuinely
    significant swing points."""
    from t3_engine.backtest.synthetic_data import generate_synthetic_series

    candles = generate_synthetic_series(num_cycles=3)
    det = ZigZagPivotDetector(deviation_pct=1.0)
    pivots = [det.update(i, c) for i, c in enumerate(candles)]
    pivots = [p for p in pivots if p is not None]

    assert len(candles) == 273
    assert len(pivots) < 50  # was 267 before the fix
    assert all(p.confirmed_at_index > p.index for p in pivots)  # never same-candle anymore


def test_bos_requires_a_minimum_significant_break():
    """Real gap found in an independent audit: on_pivot() used to fire a
    BOS/CHoCH for ANY new local high/low at all, with no floor - even a
    break a fraction of a point past the prior swing. min_break_pct gates
    event EMISSION only; the swing bookkeeping itself (last_swing_high)
    still tracks the true latest extreme either way, so a later, genuinely
    significant break off that same marginal high still fires correctly."""
    from t3_engine.common.models import Pivot
    tracker = MarketStructureTracker(min_break_pct=1.0)  # require a full 1% break
    tracker.on_pivot(Pivot(0, 0, 100, "LOW", 0))
    tracker.on_pivot(Pivot(1, 1, 110, "HIGH", 1))

    # a "higher high" that clears the prior swing by only 0.05% - below floor
    marginal = tracker.on_pivot(Pivot(2, 2, 110.05, "HIGH", 2))
    assert marginal is None
    assert tracker.last_swing_high.price == 110.05  # still tracked as the new true extreme

    # a later, genuinely significant break off that same (marginal) swing
    real_break = tracker.on_pivot(Pivot(3, 3, 115, "HIGH", 3))
    assert real_break is not None
    assert real_break.kind == "BOS"


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
