"""Behaviour of the Market Lead Engine's feature modules.

The architectural rules live in test_lead_engine_isolation.py; this file
is about what the numbers mean. Several of these tests exist because the
thing they check was WRONG at some point during the build, and the
comment says which - a test whose motivation is recorded is a test nobody
deletes as redundant three months later.
"""

import time

import pytest

from t3_engine.lead_engine import config as le_config
from t3_engine.lead_engine.btc_leadlag import BtcLeadLag
from t3_engine.lead_engine.bus import EventBus, TOPIC_TRADE
from t3_engine.lead_engine.candles import CandleBuilder
from t3_engine.lead_engine.cvd import CvdState
from t3_engine.lead_engine.health import Thresholds as _T, assess, StreamHealth
from t3_engine.lead_engine.liquidation_engine import (
    LiquidationEngine,
    liquidation_from_message,
    side_is_long_liquidation,
)
from t3_engine.lead_engine.microprice import MicropriceState
from t3_engine.lead_engine.oi_engine import OpenInterestPoller, OpenInterestState
from t3_engine.lead_engine.orderbook_engine import OrderBook
from t3_engine.lead_engine.prebreak_engine import (
    LONG,
    PreBreakEngine,
    PreBreakInputs,
    SHORT,
)
from t3_engine.lead_engine.pressure_engine import score as pressure_score
from t3_engine.lead_engine.signal_machine import SignalInputs, SignalMachine
from t3_engine.lead_engine.smc_engine import Candle, SmcEngine, find_swings
from t3_engine.lead_engine.storage import Storage
from t3_engine.lead_engine.trade_flow import Trade, TradeFlow


def book_message(kind, update_id, bids, asks, ts=1_000):
    return {"topic": "orderbook.50.TESTUSDT", "type": kind, "ts": ts,
            "data": {"u": update_id, "b": bids, "a": asks}}


def synced_book(depth=50):
    book = OrderBook("TESTUSDT", depth)
    bids = [[str(round(10.00 - i * 0.01, 4)), "100"] for i in range(depth)]
    asks = [[str(round(10.02 + i * 0.01, 4)), "100"] for i in range(depth)]
    book.apply(book_message("snapshot", 1, bids, asks))
    return book


# ---- the order book -----------------------------------------------------

def test_a_delta_over_a_sequence_gap_desyncs_rather_than_being_applied():
    """The whole reason the book is rebuilt from the stream rather than
    polled. A delta applied over a gap gives a book that LOOKS fine and
    quietly is not, and nothing downstream can tell."""
    book = synced_book()
    assert book.apply(book_message("delta", 2, [["10.00", "50"]], [])) is True
    assert book.apply(book_message("delta", 7, [["10.00", "10"]], [])) is False
    assert book.synced is False and book.gaps == 1
    assert book.metrics().synced is False
    assert book.metrics().best_bid is None, "no numbers are offered from an untrusted book"


def test_a_repeated_update_id_is_ignored_without_desyncing():
    book = synced_book()
    book.apply(book_message("delta", 2, [["10.00", "50"]], []))
    assert book.apply(book_message("delta", 2, [["10.00", "1"]], [])) is False
    assert book.synced is True, "a repeat is harmless, not a gap"


def test_a_crossed_book_desyncs():
    """Found in replay: a fixture that never removed stale bids produced a
    book quoting 5.90 bid against a 5.65 ask, with contiguous update ids
    and a perfectly well-formed imbalance computed from it. The sequence
    check cannot see this, so it is checked directly."""
    book = synced_book()
    assert book.apply(book_message("delta", 2, [["10.50", "5"]], [])) is False
    assert book.synced is False and book.crossings == 1
    bids = [["10.00", "100"]]
    asks = [["10.02", "100"]]
    assert book.apply(book_message("snapshot", 9, bids, asks)) is True
    assert book.synced is True


def test_obi_is_reported_at_every_depth_the_brief_names():
    book = synced_book()
    metrics = book.metrics()
    assert set(metrics.obi) == {"obi1", "obi5", "obi10", "obi25", "obi50"}
    assert all(-1.0 <= value <= 1.0 for value in metrics.obi.values())


def test_obi_signs_follow_the_heavier_side():
    book = OrderBook("TESTUSDT", 5)
    book.apply(book_message("snapshot", 1,
                            [["10.00", "900"], ["9.99", "900"]],
                            [["10.02", "10"], ["10.03", "10"]]))
    assert book.obi(5) > 0.9
    book.apply(book_message("snapshot", 2,
                            [["10.00", "10"]], [["10.02", "900"]]))
    assert book.obi(5) < -0.9


def test_the_microprice_leans_toward_the_side_with_less_size():
    """A huge bid and a thin offer means the next print is likelier at the
    offer, so the microprice sits ABOVE the midpoint. Getting this
    backwards is the classic microprice bug."""
    book = OrderBook("TESTUSDT", 1)
    book.apply(book_message("snapshot", 1, [["10.00", "1000"]], [["10.02", "10"]]))
    assert book.microprice() > book.midpoint()
    book.apply(book_message("snapshot", 2, [["10.00", "10"]], [["10.02", "1000"]]))
    assert book.microprice() < book.midpoint()


def test_size_that_was_traded_is_not_counted_as_size_that_was_pulled():
    """The distinction the pre-break score leans on hardest. Without
    note_trade, every execution reads as a cancellation and the engine
    reports constant 'pulling' in exactly the conditions - heavy
    trading - where pulling is supposed to mean something."""
    traded = synced_book()
    traded.note_trade("Sell", 60.0)          # a taker sell hits bids
    traded.apply(book_message("delta", 2, [["10.00", "40"]], [], ts=2_000))
    assert traded.metrics().bid_pulling == 0.0

    cancelled = synced_book()
    cancelled.apply(book_message("delta", 2, [["10.00", "40"]], [], ts=2_000))
    assert cancelled.metrics().bid_pulling > 0.0


# ---- trade flow and CVD -------------------------------------------------

def test_every_window_the_brief_names_is_reported():
    flow = TradeFlow("TESTUSDT")
    flow.add(Trade(1_000_000, 10.0, 5.0, True))
    assert set(flow.all_windows()) == {"250ms", "1s", "3s", "5s", "15s", "30s", "60s", "5m"}


def test_delta_ratio_survives_a_window_with_no_sellers():
    flow = TradeFlow("TESTUSDT")
    flow.add(Trade(1_000_000, 10.0, 5.0, True))
    assert flow.flow(1_000).delta_ratio > 0, "must not divide by zero"


def test_velocity_zscore_is_measured_against_this_instrument_not_a_constant():
    """Forty trades a second is dead for BTCUSDT and a stampede for
    FILUSDT, so the threshold cannot be absolute."""
    import random

    rnd = random.Random(19)
    flow = TradeFlow("TESTUSDT")
    stamp = 1_000_000
    # Irregular, not periodic. A perfectly even tape has zero variance,
    # which makes every z-score enormous and the test meaningless - the
    # first version of this fixture did exactly that.
    for _ in range(700):
        stamp += rnd.randint(40, 260)
        flow.add(Trade(stamp, 10.0, 1.0, True))
    quiet = flow.velocity_zscore()
    assert abs(quiet) < 3, "an ordinary tape is not an outlier against itself"
    for _ in range(300):                      # then a burst
        stamp += 5
        flow.add(Trade(stamp, 10.0, 1.0, True))
    assert flow.velocity_zscore() > quiet + 2
    assert flow.velocity_state() in ("elevated", "extreme")


def test_cvd_names_the_divergence_rather_than_signing_it():
    """'Price and flow disagree' is a different statement from 'flow is
    weak', so the four relationships are named."""
    state = CvdState("TESTUSDT")
    stamp = 1_000_000
    for i in range(120):
        stamp += 100
        # price climbing while sellers cross
        state.add(Trade(stamp, 10.0 + i * 0.001, 2.0, is_taker_buy=(i % 6 == 0)))
    assert state.relationship(15_000) == "price_up_cvd_down"
    assert state.divergence() == "price_up_cvd_down"


# ---- liquidations -------------------------------------------------------

def test_a_sell_liquidation_closes_a_long():
    """Bybit sends the ORDER's side. Closing a long means selling, so
    S='Sell' is a long being liquidated. Inverting this inverts every
    state in the module."""
    assert side_is_long_liquidation("Sell") is True
    assert side_is_long_liquidation("Buy") is False
    event = liquidation_from_message({"T": 1, "p": "10", "v": "5", "S": "Sell"}, "TESTUSDT")
    assert event is not None and event.is_long is True


def test_a_flush_is_a_cascade_while_it_accelerates_and_exhaustion_once_it_stops():
    engine = LiquidationEngine("TESTUSDT")
    stamp = 1_000_000
    for _ in range(60):
        stamp += 100
        engine.add(liquidation_from_message(
            {"T": stamp, "p": "5.8", "v": "1200", "S": "Sell"}, "TESTUSDT"))
    assert engine.state() == "CASCADE"
    assert engine.pressure_component() < 0, "a long flush is selling while it runs"

    stamp += 20_000
    engine.add(liquidation_from_message(
        {"T": stamp, "p": "5.8", "v": "2", "S": "Sell"}, "TESTUSDT"))
    assert engine.state() == "EXHAUSTION"
    assert engine.pressure_component() > 0, "the weak side has been removed"


def test_exhaustion_is_measured_against_the_peak_not_the_minute_average():
    """A four-second flush of 90k averaged over sixty seconds is 1.5k/s,
    below every sensible threshold - comparing that average to the
    threshold made EXHAUSTION unreachable, which is how this was found."""
    engine = LiquidationEngine("TESTUSDT")
    stamp = 1_000_000
    for _ in range(40):
        stamp += 100
        engine.add(liquidation_from_message(
            {"T": stamp, "p": "5.8", "v": "1500", "S": "Buy"}, "TESTUSDT"))
    peak = engine.peak_velocity()
    assert peak > engine.window(60_000)["velocity"] * 5


# ---- open interest ------------------------------------------------------

@pytest.mark.parametrize("prices,ois,expected", [
    ([10.0, 10.5], [100.0, 110.0], "new_longs_expansion"),
    ([10.0, 10.5], [110.0, 100.0], "short_covering"),
    ([10.5, 10.0], [100.0, 110.0], "new_shorts"),
    ([10.5, 10.0], [110.0, 100.0], "long_liquidation_deleveraging"),
])
def test_the_four_open_interest_readings(prices, ois, expected):
    state = OpenInterestState("TESTUSDT")
    for index, (price, oi) in enumerate(zip(prices, ois)):
        state.observe(1_000_000 + index * 300_000, oi, price)
    assert state.interpretation() == expected


def test_open_interest_polling_is_a_thread_of_its_own():
    """The brief asks for it off the event loop; the reason is that a REST
    endpoint that hangs must not hold up the order book."""
    seen = []
    poller = OpenInterestPoller(["TESTUSDT"], "http://example.invalid",
                                lambda *args: seen.append(args),
                                fetcher=lambda s, b: {"open_interest": 5.0, "timestamp_ms": 7})
    assert poller.poll_once() == 1
    assert seen == [("TESTUSDT", 5.0, 7)]


# ---- BTC lead-lag -------------------------------------------------------

def test_the_lag_of_a_known_follower_is_recovered():
    """A synthetic altcoin that copies BTC three buckets later must be
    measured at three buckets."""
    import math
    import random

    rnd = random.Random(3)
    lead_lag = BtcLeadLag()
    btc, alt, steps = 60_000.0, 5.8, []
    stamp = 1_000_000
    for i in range(200):
        step = rnd.gauss(0, 0.0008)
        steps.append(step)
        btc *= math.exp(step)
        lead_lag.observe("BTCUSDT", stamp, btc)
        alt *= math.exp((steps[-4] if len(steps) >= 4 else 0.0) * 1.6 + rnd.gauss(0, 0.0003))
        lead_lag.observe("ALTUSDT", stamp, alt)
        stamp += 1_000
    state = lead_lag.state_for("ALTUSDT")
    assert state.lag_buckets == 3
    assert state.correlation > 0.8


def test_btc_does_not_lead_itself():
    lead_lag = BtcLeadLag()
    for i in range(50):
        lead_lag.observe("BTCUSDT", 1_000_000 + i * 1_000, 60_000 + i)
    state = lead_lag.state_for("BTCUSDT")
    assert state.correlation is None and state.lead_score == 0.0


# ---- structure ----------------------------------------------------------

def test_a_forming_candle_can_never_create_a_swing():
    """The no-lookahead guarantee at the structure level. A swing needs
    bars on BOTH sides, so the newest bar cannot be one."""
    engine = SmcEngine("TESTUSDT", "1")
    for i, price in enumerate([5.0, 5.1, 5.2, 5.3, 5.4, 5.3, 5.2]):
        engine.update(Candle(i * 60_000, price, price + 0.01, price - 0.01, price, 1.0, True))
    before = engine.state().swing_high
    engine.update(Candle(7 * 60_000, 5.2, 99.0, 5.0, 98.0, 1.0, closed=False))
    assert engine.state().swing_high == before


def test_a_republished_forming_candle_replaces_rather_than_appends():
    """Bybit resends the forming bar on every tick with the same `start`.
    Appending would fill the series with copies of one bar and destroy
    every swing calculation."""
    engine = SmcEngine("TESTUSDT", "1")
    for _ in range(20):
        engine.update(Candle(0, 5.0, 5.1, 4.9, 5.05, 1.0, closed=False))
    assert len(engine.candles) == 1


def test_candles_are_built_from_trades_so_structure_exists_before_klines_do():
    """Bybit's kline topic pushes the current bar, not history, so an
    engine relying on it alone has no swings for its first minutes."""
    builder = CandleBuilder("TESTUSDT", 15_000)
    stamp = 1_000_000
    for i in range(100):
        builder.add(Trade(stamp + i * 1_000, 10.0 + (i % 5) * 0.01, 1.0, True))
    assert builder.closed_count() >= 5
    assert all(c.closed for c in builder.candles)
    assert builder.forming() is not None and builder.forming().closed is False


# ---- pressure -----------------------------------------------------------

def _layers(**scores):
    """Five LayerScores from a dict of signed scores. `None` means the
    layer had nothing to say, which is not the same as zero."""
    from t3_engine.lead_engine.layers import LAYERS, LayerScore

    out = {}
    for name in LAYERS:
        value = scores.get(name)
        out[name] = LayerScore(name=name, score=value or 0.0,
                               confidence=0.0 if value is None else 1.0)
    return out


def test_long_and_short_pressure_are_independent_not_complements():
    """A featureless market must not read as 50 long. And a market being
    fought over must read as high on BOTH, which one meter cannot show."""
    quiet = pressure_score(_layers(flow=0.0, book=0.0, structure=0.0,
                                   derivatives=0.0, btc_lead=0.0))
    assert quiet.long_pressure == 0.0 and quiet.short_pressure == 0.0

    contested = pressure_score(_layers(flow=0.9, book=-0.9, structure=0.9,
                                       derivatives=-0.9, btc_lead=0.0))
    assert contested.long_pressure > 20 and contested.short_pressure > 20


def test_a_layer_that_is_not_ready_is_dropped_not_counted_as_neutral():
    """Counting a missing stream as zero dilutes a genuine reading toward
    the middle, making a half-connected engine look calm rather than
    uninformed. Its weight is redistributed and its name reported."""
    partial = pressure_score(_layers(flow=1.0))
    assert partial.long_pressure == pytest.approx(100.0)
    assert sorted(partial.missing) == ["book", "btc_lead", "derivatives", "structure"]
    # ...but the engine says it is not sure, because four fifths of the
    # weight had nothing to contribute.
    assert partial.confidence < 0.35


def test_independent_layers_disagreeing_is_detected_and_costs_confidence():
    """The brief's case: structure bullish, flow and book bearish. The old
    `conflict` was min(long, short) - it noticed both sides had scored but
    not WHICH sources disagreed, so this situation and nine mildly mixed
    components produced the same number."""
    from t3_engine.lead_engine.layers import CONFLICT_HIGH

    result = pressure_score(_layers(structure=0.8, flow=-0.7, book=-0.6,
                                    derivatives=0.0, btc_lead=-0.2))
    assert result.conflict.level == CONFLICT_HIGH
    assert result.conflict.opposing == ["structure"]
    assert result.confidence < 0.5
    unanimous = pressure_score(_layers(structure=0.8, flow=0.7, book=0.6,
                                       derivatives=0.4, btc_lead=0.2))
    assert unanimous.confidence > result.confidence * 2


def test_the_published_weights_sum_to_one():
    assert le_config.PressureWeights().total() == pytest.approx(1.0)
    assert le_config.LayerWeights().total() == pytest.approx(1.0)


# ---- pre-break ----------------------------------------------------------

def _prebreak_inputs(price, book_metrics, **kwargs):
    defaults = dict(microprice_offset_bps=-2.0, cvd_component=-0.8, flow_component=-0.8,
                    velocity_zscore=2.5, velocity_acceleration=5.0, btc_lead_score=-0.6)
    defaults.update(kwargs)
    return PreBreakInputs(price=price, book=book_metrics, **defaults)


def _pressured_book():
    from t3_engine.lead_engine.orderbook_engine import OrderBookMetrics
    return OrderBookMetrics(
        symbol="TESTUSDT", synced=True, updated_at_ms=1, best_bid=5.700, best_ask=5.702,
        spread=0.002, midpoint=5.701, microprice=5.7004, bid_depth=1000, ask_depth=6000,
        imbalance=-0.7, obi={"obi5": -0.7}, weighted_obi=-0.7,
        bid_pulling=900.0, ask_pulling=50.0, bid_replenishment=100.0,
        ask_replenishment=1600.0, stacked_ask_levels=4)


def _tested_support_candles():
    prices = [5.76, 5.72, 5.700, 5.745, 5.715, 5.700, 5.728, 5.710, 5.700,
              5.716, 5.705, 5.700, 5.702]
    return [Candle(i * 15_000, p, p + 0.002, p - 0.002, p, 10.0, True)
            for i, p in enumerate(prices)]


def test_a_pre_break_warning_is_produced_before_the_level_gives_way():
    engine = PreBreakEngine("TESTUSDT")
    result = engine.evaluate(SHORT, _prebreak_inputs(5.7005, _pressured_book(),
                                                     candles=_tested_support_candles()))
    assert result.level is not None
    assert result.probability > 50, result.as_dict()
    assert result.features["defender_pulling"] > 0.5
    assert result.features["attacker_stacking"] > 0.5
    assert result.tests >= 2


def test_identical_order_flow_far_from_the_level_scores_much_lower():
    """Compression gates the score. A perfect order-flow reading taken
    while price is nowhere near the level is a description of the market,
    not a warning about that level."""
    engine = PreBreakEngine("TESTUSDT")
    candles = _tested_support_candles()
    near = engine.evaluate(SHORT, _prebreak_inputs(5.7005, _pressured_book(), candles=candles))
    far = engine.evaluate(SHORT, _prebreak_inputs(5.95, _pressured_book(), candles=candles))
    assert far.probability < near.probability / 2


def test_the_same_reading_does_not_also_call_a_long_break():
    engine = PreBreakEngine("TESTUSDT")
    candles = _tested_support_candles()
    short = engine.evaluate(SHORT, _prebreak_inputs(5.7005, _pressured_book(), candles=candles))
    long = engine.evaluate(LONG, _prebreak_inputs(5.7005, _pressured_book(), candles=candles))
    assert long.probability < short.probability


def test_bounces_are_measured_between_visits_not_on_every_bar_at_the_level():
    """The first version recorded a bounce for each consecutive bar
    sitting on the level, each measuring zero, and the feature silently
    reported 0.0 for a level being tested three times."""
    engine = PreBreakEngine("TESTUSDT")
    engine.levels.rebuild(_tested_support_candles())
    support = next(lv for lv in engine.levels.levels if lv.kind == "support")
    assert len(support.bounces) >= 2
    assert support.bounces == sorted(support.bounces, reverse=True), "they shrink"
    assert PreBreakEngine._fading_bounces(support) > 0.5


def test_no_probability_is_offered_from_an_unsynced_book():
    from t3_engine.lead_engine.orderbook_engine import OrderBookMetrics
    engine = PreBreakEngine("TESTUSDT")
    broken = OrderBookMetrics(symbol="TESTUSDT", synced=False, updated_at_ms=1)
    result = engine.evaluate(SHORT, _prebreak_inputs(5.7005, broken,
                                                     candles=_tested_support_candles()))
    assert result.probability == 0.0 and "not synced" in result.note


# ---- signals and health -------------------------------------------------

def _signal_inputs(**kwargs):
    base = dict(long_pressure=5.0, short_pressure=5.0, conflict=0.0, prebreak_long=0.0,
                prebreak_short=0.0, long_level=None, short_level=5.7,
                liquidation_state="NEUTRAL", healthy=True)
    base.update(kwargs)
    return SignalInputs(**base)


def test_the_signal_machine_walks_the_whole_lifecycle():
    machine = SignalMachine("TESTUSDT")
    assert machine.update(_signal_inputs(), now=0).state == "IDLE"
    assert machine.update(_signal_inputs(short_pressure=40), now=1).state == "WATCH"
    assert machine.update(_signal_inputs(short_pressure=60), now=2).state == "PRE_SIGNAL"
    assert machine.update(_signal_inputs(short_pressure=62, prebreak_short=65),
                          now=3).state == "PRE_BREAK_SHORT"
    assert machine.update(_signal_inputs(short_pressure=70, prebreak_short=75),
                          now=4).state == "HIGH_PROBABILITY"
    assert machine.update(_signal_inputs(short_pressure=72, prebreak_short=90),
                          now=5).state == "A_PLUS"
    assert machine.update(_signal_inputs(short_pressure=5, prebreak_short=2),
                          now=6).state == "INVALIDATED"


def test_a_degraded_feed_produces_no_signal_at_all():
    """Not a cautious one. Every number is suspect at exactly the moment
    it would be most tempting to act on one."""
    machine = SignalMachine("TESTUSDT")
    machine.update(_signal_inputs(short_pressure=80, prebreak_short=95), now=0)
    result = machine.update(_signal_inputs(short_pressure=80, prebreak_short=95,
                                           healthy=False, health_reason="book desynced"), now=1)
    assert result.state == "DATA_FAILURE"
    assert "desynced" in result.reason


def test_the_direction_comes_from_pressure_when_no_level_is_under_stress():
    """With both pre-break probabilities at zero, deciding the side on
    them alone described a market with 60 short pressure as 'long',
    because zero is not less than zero."""
    machine = SignalMachine("TESTUSDT")
    result = machine.update(_signal_inputs(short_pressure=60, long_pressure=4), now=0)
    assert result.direction == "short"


def test_an_invalidation_holds_before_decaying():
    machine = SignalMachine("TESTUSDT")
    machine.update(_signal_inputs(short_pressure=70, prebreak_short=80), now=0)
    machine.update(_signal_inputs(short_pressure=2, prebreak_short=1), now=1)
    assert machine.update(_signal_inputs(short_pressure=70, prebreak_short=80),
                          now=2).state == "INVALIDATED"
    assert machine.update(_signal_inputs(short_pressure=70, prebreak_short=80),
                          now=200).state != "INVALIDATED"


def test_missing_liquidations_do_not_degrade_the_feed():
    """Most instruments go minutes without one; treating that as a fault
    would mute the engine permanently on the quiet half of the list."""
    now_ms = int(time.time() * 1000)
    health = StreamHealth("TESTUSDT", ws_connected=True, orderbook_synced=True,
                          last_book_ms=now_ms - 100, last_trade_ms=now_ms - 500,
                          last_ticker_ms=now_ms - 500)
    assert assess(health, _T(), now_ms).status == "OK"


def _health(now_ms, book_age, trade_age=500, **kwargs):
    """A StreamHealth whose book and trade frames landed that long ago."""
    health = StreamHealth("TESTUSDT", ws_connected=kwargs.pop("ws_connected", True),
                          orderbook_synced=kwargs.pop("orderbook_synced", True))
    health.last_book_ms = health.last_book_receive_ms = now_ms - book_age
    health.last_trade_ms = health.last_trade_receive_ms = now_ms - trade_age
    health.last_ticker_ms = now_ms - 500
    for name, value in kwargs.items():
        setattr(health, name, value)
    return health


def test_a_socket_that_is_up_carrying_old_data_is_never_called_ok():
    """"Connected" and "current" are not the same claim, and the first
    build made the first while meaning the second - which is how a header
    came to read "feed OK" beside a nine-second book age."""
    from t3_engine.lead_engine.health import DEGRADED, WS_CONNECTED_DATA_STALE

    now_ms = int(time.time() * 1000)
    verdict = assess(_health(now_ms, 20_000), _T(), now_ms)
    assert verdict.status == WS_CONNECTED_DATA_STALE
    assert verdict.signals_enabled is False

    # A socket that is down is DEGRADED, not stale: nothing is old, there
    # is simply nothing.
    down = _health(now_ms, 100, ws_connected=False)
    assert assess(down, _T(), now_ms).status == DEGRADED


def test_the_freshness_thresholds_are_the_two_decisions_they_claim_to_be():
    """A book a second old is shown but not trusted; a book two and a
    half seconds old buys no opinion at all. On a perpetual whose book
    ticks every 20-100ms, neither number is harsh."""
    from t3_engine.lead_engine.health import OK, WS_CONNECTED_DATA_STALE

    now_ms = int(time.time() * 1000)
    thresholds = _T()

    fresh = assess(_health(now_ms, 200, 400), thresholds, now_ms)
    assert fresh.status == OK and fresh.signals_enabled is True

    degraded = assess(_health(now_ms, 1_400, 400), thresholds, now_ms)
    assert degraded.status == WS_CONNECTED_DATA_STALE
    assert degraded.signals_enabled is False

    muted = assess(_health(now_ms, 3_000, 400), thresholds, now_ms)
    assert muted.signals_enabled is False

    no_trades = assess(_health(now_ms, 200, 2_000), thresholds, now_ms)
    assert no_trades.signals_enabled is False
    assert any("no trade" in reason for reason in no_trades.reasons)


def test_a_desynced_book_says_why_rather_than_only_that():
    """"Order book not synced" is not a diagnosis. The sequence that broke
    is one, and the resync that follows depends on knowing which."""
    now_ms = int(time.time() * 1000)
    health = _health(now_ms, 200, orderbook_synced=False)
    health.sequence = {"desync_reason": "sequence gap: expected 12, got 19",
                       "sequence_gaps": 1, "resync_count": 1}
    verdict = assess(health, _T(), now_ms)
    assert verdict.signals_enabled is False
    assert any("expected 12, got 19" in reason for reason in verdict.reasons)


def test_the_four_clocks_measure_four_different_things():
    """The defect this replaces: one number printed under two names, so
    "latency 3334ms" and "book 3.3s" in the header were the same
    staleness twice. The case that hid is a FAST link carrying OLD data.

    The four are now stored apart - exchange, receive, process, ui - so
    every age is measured from the clock that answers its own question."""
    now_ms = int(time.time() * 1000)
    health = StreamHealth("TESTUSDT", ws_connected=True, orderbook_synced=True)
    health.last_book_ms = now_ms - 9_000          # exchange said 9s ago
    health.last_book_receive_ms = now_ms - 8_960  # it landed 40ms later
    health.last_trade_ms = health.last_trade_receive_ms = now_ms - 400
    health.last_ticker_ms = now_ms - 400
    health.last_ui_ms = now_ms - 250
    health.network_latency_ms = 40.0
    health.processing_latency_ms = 0.6

    payload = health.as_dict(now_ms)
    assert payload["network_latency_ms"] == 40.0
    assert payload["processing_latency_ms"] == 0.6
    assert payload["book_age_ms"] == pytest.approx(8_960, abs=50)
    assert payload["book_data_age_ms"] == pytest.approx(9_000, abs=50)
    assert payload["trade_age_ms"] == pytest.approx(400, abs=50)
    assert payload["ui_age_ms"] == pytest.approx(250, abs=50)
    # The whole point: a fast link carrying old data.
    assert payload["network_latency_ms"] < payload["book_age_ms"] / 100
    # And the old names still mean what they always meant.
    assert payload["ws_latency_ms"] == payload["network_latency_ms"]
    assert payload["processing_ms"] == payload["processing_latency_ms"]


# ---- bus and storage ----------------------------------------------------

def test_the_bus_refuses_a_topic_from_another_namespace():
    bus = EventBus()
    bus.publish(TOPIC_TRADE, {"symbol": "TESTUSDT"})
    with pytest.raises(ValueError):
        bus.publish("elliott.trade", {})
    with pytest.raises(ValueError):
        bus.publish("lead_engine.invented", {})


def test_a_throwing_subscriber_does_not_stop_the_others():
    bus = EventBus()
    seen = []
    bus.subscribe(TOPIC_TRADE, lambda t, p: (_ for _ in ()).throw(RuntimeError("boom")))
    bus.subscribe(TOPIC_TRADE, lambda t, p: seen.append(p))
    bus.publish(TOPIC_TRADE, {"symbol": "TESTUSDT"})
    assert seen == [{"symbol": "TESTUSDT"}]


def test_storage_writes_only_to_its_own_tables():
    store = Storage()
    with pytest.raises(ValueError):
        store.record("analysis_cache", {})
    with pytest.raises(ValueError):
        store.record("candles", {})
    store.record("lead_engine_signals", {"symbol": "TESTUSDT"})
    assert store.stats()["buffered"]["lead_engine_signals"] == 1


# ---- microprice ---------------------------------------------------------

def test_microprice_deltas_cover_the_four_horizons():
    state = MicropriceState("TESTUSDT")
    for i in range(60):
        state.observe(1_000 + i * 100, 5.80 + i * 0.00005, 5.80)
    deltas = state.deltas()
    assert set(deltas) == {"microprice_delta_250ms", "microprice_delta_1s",
                           "microprice_delta_3s", "microprice_delta_5s"}
    assert state.bias() == "bullish"
    assert state.pressure_component() > 0


# ---- concurrency: the socket thread and a request thread at once --------

def test_reading_state_while_the_tape_runs_never_raises():
    """The failure this pins down was a 500 under load, not a crash in a
    test: the socket thread appends to a TimeSeries deque while a request
    thread walks the same deque to answer /state, and CPython raises
    `RuntimeError: deque mutated during iteration`. Intermittent, load
    dependent, and invisible until a browser is actually polling."""
    import threading

    from t3_engine.lead_engine.config import LeadEngineConfig
    from t3_engine.lead_engine.engine import LeadEngine

    engine = LeadEngine(LeadEngineConfig(enabled=True, symbols=["BTCUSDT", "INJUSDT"]))
    now = int(time.time() * 1000)
    tick = 0.001
    price = 5.70
    engine.handle_message("orderbook.50.INJUSDT", {
        "topic": "orderbook.50.INJUSDT", "type": "snapshot", "ts": now,
        "data": {"u": 1,
                 "b": [[f"{price - (i + 1) * tick:.4f}", "100"] for i in range(50)],
                 "a": [[f"{price + (i + 1) * tick:.4f}", "60"] for i in range(50)]}})

    stop = threading.Event()
    failures = []

    def ingest():
        update_id = 1
        index = 0
        try:
            while not stop.is_set():
                stamp = int(time.time() * 1000)
                update_id += 1
                index += 1
                engine.handle_message("publicTrade.INJUSDT", {
                    "topic": "publicTrade.INJUSDT", "ts": stamp,
                    "data": [{"T": stamp, "S": "Buy" if index % 2 else "Sell",
                              "v": "3", "p": f"{price:.4f}"}]})
                engine.handle_message("orderbook.50.INJUSDT", {
                    "topic": "orderbook.50.INJUSDT", "type": "delta", "ts": stamp,
                    "data": {"u": update_id,
                             "b": [[f"{price - tick:.4f}", f"{100 + index % 7}"]],
                             "a": [[f"{price + tick:.4f}", f"{60 + index % 5}"]]}})
        except Exception as exc:                      # noqa: BLE001
            failures.append(("ingest", exc))

    def read():
        try:
            for _ in range(200):
                engine.get_state("INJUSDT", force=True)
        except Exception as exc:                      # noqa: BLE001
            failures.append(("read", exc))

    writer = threading.Thread(target=ingest, daemon=True)
    writer.start()
    readers = [threading.Thread(target=read) for _ in range(3)]
    for reader in readers:
        reader.start()
    for reader in readers:
        reader.join(timeout=60)
    stop.set()
    writer.join(timeout=5)

    assert not failures, failures


def test_btc_flow_is_read_from_btcs_own_frame_not_rescored():
    """Re-scoring BTC's flow for every tracked symbol pushed the same
    observation into BTC's rolling normaliser once per symbol per tick,
    which corrupts BTC's own z-scores."""
    from t3_engine.lead_engine.config import LeadEngineConfig
    from t3_engine.lead_engine.engine import LeadEngine

    engine = LeadEngine(LeadEngineConfig(
        enabled=True, symbols=["BTCUSDT", "INJUSDT", "SOLUSDT"]))
    now = int(time.time() * 1000)
    for index in range(40):
        stamp = now + index * 100
        engine.handle_message("publicTrade.BTCUSDT", {
            "topic": "publicTrade.BTCUSDT", "ts": stamp,
            "data": [{"T": stamp, "S": "Buy", "v": "1", "p": "64000"}]})

    btc = engine.states["BTCUSDT"]
    feature = btc.normalizer.feature("cvd_slope_60s")
    engine.get_state("BTCUSDT", force=True)
    after_own = len(feature.samples)
    # Two OTHER symbols each take a snapshot. Neither may add a sample to
    # BTC's history.
    engine.get_state("INJUSDT", force=True)
    engine.get_state("SOLUSDT", force=True)
    assert len(feature.samples) == after_own


def test_the_recorder_reports_the_feed_rate_between_sweeps(caplog):
    """The ingest rate was only visible to whoever could open
    /api/lead-engine/status in a browser, which is no help when the
    question is what the feed was doing an hour ago."""
    import logging

    from t3_engine.lead_engine.config import LeadEngineConfig
    from t3_engine.lead_engine.engine import LeadEngine
    from t3_engine.lead_engine.storage import Recorder, Storage

    engine = LeadEngine(LeadEngineConfig(enabled=True, symbols=["INJUSDT"]))
    recorder = Recorder(engine, Storage(), interval_seconds=15.0)

    # No socket yet: a heartbeat must not be the thing that breaks a sweep.
    engine.stream = None
    recorder.sweep_once()

    class _Stream:
        def __init__(self):
            from t3_engine.lead_engine.bybit_ws import StreamStats

            self.stats = StreamStats()
            self.stats.connected = True

    engine.stream = _Stream()
    with caplog.at_level(logging.INFO, logger="t3_engine.lead_engine.storage"):
        recorder.sweep_once()          # no previous sweep: rate unknown
        engine.stream.stats.messages = 900
        recorder.sweep_once()

    lines = [r.message for r in caplog.records if "lead_engine feed" in r.message]
    assert len(lines) == 2
    assert "rate=?/s" in lines[0], lines[0]
    assert "messages=900" in lines[1] and "rate=?/s" not in lines[1], lines[1]
    # Everything BUILD-CHECK-044 item 11 measures has to be readable from
    # the deployment's own logs, because that is the only place a
    # thirty-minute live run can be checked from.
    for field in ("book_age_median=", "book_age_p95=", "gaps=", "resyncs=",
                  "data_failure=", "queue=", "dropped=", "net_latency=",
                  "queue_wait="):
        assert field in lines[1], f"{field} missing from {lines[1]}"
