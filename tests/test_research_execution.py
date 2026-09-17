"""The simulator every result depends on.

Its failure modes are all generous ones - a fill that was too early, too
big, too cheap, or that ignored a rule the venue enforces - and each of
them makes a losing strategy look profitable. So each gets a test that
would catch it.
"""

import pytest

from t3_engine.research.book import BUY, SELL, BookState, Trade
from t3_engine.research.costs import MAKER, TAKER, FeeSchedule
from t3_engine.research.execution import (CANCELLED, FILLED, LIMIT, MARKET, OPEN,
                                          PARTIAL, PENDING, REJECTED,
                                          ExecutionSimulator, InstrumentSpec,
                                          LatencyModel, QueueModel)

SPEC = InstrumentSpec("INJUSDT", tick_size=0.001, qty_step=0.1, min_qty=0.1,
                      min_notional=1.0)


def _book(ms, bids=None, asks=None):
    return BookState(exchange_ms=ms, recv_ms=ms,
                     bids=bids if bids is not None else [(5.750, 100.0), (5.749, 200.0)],
                     asks=asks if asks is not None else [(5.751, 80.0), (5.752, 150.0)])


def _sim(**kwargs):
    kwargs.setdefault("latency", LatencyModel(send_ms=100, ack_ms=50, cancel_ms=100))
    kwargs.setdefault("queue", QueueModel.conservative())
    kwargs.setdefault("max_participation", 1.0)
    sim = ExecutionSimulator(SPEC, **kwargs)
    sim.on_book(_book(1000))
    return sim


# ---- causality ----------------------------------------------------------

def test_an_order_cannot_fill_before_it_could_have_arrived():
    """The single rule the whole module exists to enforce."""
    sim = _sim()
    order = sim.submit(BUY, 10.0, kind=MARKET, decided_at_ms=1000)
    assert order.state == PENDING and order.arrives_at_ms == 1100

    # A book and a tape BEFORE arrival must leave it untouched.
    sim.on_book(_book(1050))
    sim.on_trade(Trade(exchange_ms=1060, recv_ms=1060, price=5.751, size=999, side=BUY))
    assert order.filled_qty == 0.0 and order.state == PENDING

    sim.on_book(_book(1100))
    assert order.filled_qty == 10.0


def test_the_fill_uses_the_book_at_arrival_not_the_book_at_the_decision():
    """A market that ran away between the decision and the arrival must
    charge the strategy for the move, not hand it the old price."""
    sim = _sim()
    order = sim.submit(BUY, 10.0, kind=MARKET, decided_at_ms=1000)
    sim.on_book(_book(1200, asks=[(5.800, 500.0)]))
    assert order.avg_price == pytest.approx(5.800)


def test_a_cancel_is_not_instant_and_the_order_can_still_fill_inside_the_window():
    """Assuming an instant cancel is how a simulated strategy avoids
    every loss it would really have taken."""
    sim = _sim()
    order = sim.submit(BUY, 10.0, kind=LIMIT, price=5.750, decided_at_ms=1000)
    sim.on_book(_book(1200))
    assert order.state == OPEN

    sim.cancel(order.order_id, 1300)             # lands at 1400
    sim.on_trade(Trade(exchange_ms=1350, recv_ms=1350, price=5.740, size=5, side=SELL))
    assert order.state == FILLED
    assert sim.cancelled_after_fill == 1


def test_a_cancel_that_lands_first_does_close_the_order():
    sim = _sim()
    order = sim.submit(BUY, 10.0, kind=LIMIT, price=5.750, decided_at_ms=1000)
    sim.on_book(_book(1200))
    sim.cancel(order.order_id, 1300)
    sim.on_book(_book(1500))
    assert order.state == CANCELLED and order.filled_qty == 0.0


# ---- taker --------------------------------------------------------------

def test_a_taker_walks_real_depth_and_pays_the_worse_levels():
    sim = _sim()
    order = sim.submit(BUY, 200.0, kind=MARKET, decided_at_ms=1000)
    sim.on_book(_book(1200))
    # 80 at 5.751 then 120 at 5.752
    assert order.filled_qty == pytest.approx(200.0)
    assert order.avg_price == pytest.approx((80 * 5.751 + 120 * 5.752) / 200)
    assert order.avg_price > 5.751


def test_a_book_thinner_than_the_order_gives_a_partial_not_an_invented_fill():
    sim = _sim()
    order = sim.submit(BUY, 500.0, kind=MARKET, decided_at_ms=1000)
    sim.on_book(_book(1200))
    assert order.filled_qty == pytest.approx(230.0)      # 80 + 150, no more
    assert order.state == PARTIAL
    assert order.remaining == pytest.approx(270.0)


def test_participation_caps_how_much_of_a_level_we_may_eat():
    """Replayed data cannot show the market answering our order, so a
    fill that swallows a whole level is one we cannot defend."""
    sim = _sim(max_participation=0.25)
    order = sim.submit(BUY, 100.0, kind=MARKET, decided_at_ms=1000)
    sim.on_book(_book(1200))
    assert order.filled_qty == pytest.approx(0.25 * 80 + 0.25 * 150)


def test_a_marketable_limit_crosses_only_up_to_its_own_price():
    sim = _sim()
    order = sim.submit(BUY, 200.0, kind=LIMIT, price=5.751, decided_at_ms=1000)
    sim.on_book(_book(1200))
    assert order.filled_qty == pytest.approx(80.0)       # 5.752 is beyond its limit
    assert order.state in (OPEN, PARTIAL)


# ---- post only ----------------------------------------------------------

def test_a_post_only_order_that_is_marketable_on_arrival_is_rejected():
    """The venue rejects it. Filling it as a maker would hand the
    strategy a taker's fill at a maker's fee."""
    sim = _sim()
    order = sim.submit(BUY, 10.0, kind=LIMIT, price=5.760, post_only=True,
                       decided_at_ms=1000)
    sim.on_book(_book(1200))
    assert order.state == REJECTED
    assert "post-only" in order.reject_reason
    assert order.filled_qty == 0.0
    assert sim.rejected_post_only == 1


def test_post_only_is_judged_at_arrival_not_at_the_decision():
    """It was passive when we decided and marketable when it landed. The
    venue sees only the second."""
    sim = _sim()
    order = sim.submit(BUY, 10.0, kind=LIMIT, price=5.750, post_only=True,
                       decided_at_ms=1000)
    sim.on_book(_book(1200, bids=[(5.745, 100.0)], asks=[(5.746, 90.0)]))
    assert order.state == REJECTED


def test_a_passive_post_only_order_rests_normally():
    sim = _sim()
    order = sim.submit(BUY, 10.0, kind=LIMIT, price=5.749, post_only=True,
                       decided_at_ms=1000)
    sim.on_book(_book(1200))
    assert order.state == OPEN and sim.rejected_post_only == 0


# ---- the maker queue ----------------------------------------------------

def test_the_queue_ahead_is_taken_from_the_level_at_arrival():
    sim = _sim()
    order = sim.submit(BUY, 10.0, kind=LIMIT, price=5.750, decided_at_ms=1000)
    sim.on_book(_book(1200, bids=[(5.750, 137.0)]))
    assert order.queue_ahead == pytest.approx(137.0)
    assert order.level_size_at_arrival == pytest.approx(137.0)


def test_trades_at_our_price_eat_the_queue_before_they_reach_us():
    sim = _sim()
    order = sim.submit(BUY, 10.0, kind=LIMIT, price=5.750, decided_at_ms=1000)
    sim.on_book(_book(1200))                       # 100 resting ahead
    sim.on_trade(Trade(exchange_ms=1300, recv_ms=1300, price=5.750, size=60, side=SELL))
    assert order.filled_qty == 0.0 and order.queue_ahead == pytest.approx(40.0)
    sim.on_trade(Trade(exchange_ms=1400, recv_ms=1400, price=5.750, size=45, side=SELL))
    assert order.filled_qty == pytest.approx(5.0)   # 45 - 40 left over
    assert order.state == PARTIAL


def test_a_trade_through_our_price_fills_us_completely():
    """If the tape printed past our level, everything at it was taken."""
    sim = _sim()
    order = sim.submit(BUY, 10.0, kind=LIMIT, price=5.750, decided_at_ms=1000)
    sim.on_book(_book(1200))
    sim.on_trade(Trade(exchange_ms=1300, recv_ms=1300, price=5.745, size=1, side=SELL))
    assert order.state == FILLED and order.filled_qty == 10.0


def test_a_trade_on_the_wrong_side_never_fills_a_resting_order():
    """A buyer lifting the ask cannot fill our resting bid."""
    sim = _sim()
    order = sim.submit(BUY, 10.0, kind=LIMIT, price=5.750, decided_at_ms=1000)
    sim.on_book(_book(1200))
    sim.on_trade(Trade(exchange_ms=1300, recv_ms=1300, price=5.751, size=999, side=BUY))
    assert order.filled_qty == 0.0


def test_a_price_merely_touching_our_level_is_not_a_fill():
    """The book quoting our price proves nothing: somebody has to trade."""
    sim = _sim()
    order = sim.submit(BUY, 10.0, kind=LIMIT, price=5.750, decided_at_ms=1000)
    sim.on_book(_book(1200))
    for ms in (1300, 1400, 1500):
        sim.on_book(_book(ms, bids=[(5.750, 100.0)], asks=[(5.751, 5.0)]))
    assert order.filled_qty == 0.0 and order.state == OPEN


def test_the_conservative_model_does_not_let_cancellations_advance_us():
    """Size vanishing without a trade could have been in front of us or
    behind it. Assuming in front is the optimistic read."""
    sim = _sim(queue=QueueModel.conservative())
    order = sim.submit(BUY, 10.0, kind=LIMIT, price=5.750, decided_at_ms=1000)
    sim.on_book(_book(1200, bids=[(5.750, 100.0)]))
    sim.on_book(_book(1300, bids=[(5.750, 20.0)]))      # 80 cancelled
    assert order.queue_ahead == pytest.approx(100.0)

    optimistic = _sim(queue=QueueModel.optimistic())
    other = optimistic.submit(BUY, 10.0, kind=LIMIT, price=5.750, decided_at_ms=1000)
    optimistic.on_book(_book(1200, bids=[(5.750, 100.0)]))
    optimistic.on_book(_book(1300, bids=[(5.750, 20.0)]))
    assert other.queue_ahead < 100.0 * QueueModel.optimistic().queue_factor


def test_the_optimistic_queue_factor_only_ever_helps():
    """Stated as a property so the two readings cannot silently swap."""
    results = {}
    for name, queue in (("conservative", QueueModel.conservative()),
                        ("optimistic", QueueModel(queue_factor=0.5,
                                                  cancel_helps=False))):
        sim = _sim(queue=queue)
        order = sim.submit(BUY, 10.0, kind=LIMIT, price=5.750, decided_at_ms=1000)
        sim.on_book(_book(1200))
        sim.on_trade(Trade(exchange_ms=1300, recv_ms=1300, price=5.750, size=55,
                           side=SELL))
        results[name] = order.filled_qty
    assert results["optimistic"] > results["conservative"] == 0.0


# ---- instrument rules ---------------------------------------------------

def test_a_resting_buy_rounds_down_so_rounding_never_makes_it_cross():
    sim = _sim()
    order = sim.submit(BUY, 10.0, kind=LIMIT, price=5.7509, decided_at_ms=1000)
    assert order.price == pytest.approx(5.750)
    sell = sim.submit(SELL, 10.0, kind=LIMIT, price=5.7501, decided_at_ms=1000)
    assert sell.price == pytest.approx(5.751)


def test_quantity_rounds_down_never_up():
    sim = _sim()
    order = sim.submit(BUY, 1.99, kind=LIMIT, price=5.750, decided_at_ms=1000)
    assert order.qty == pytest.approx(1.9)


def test_an_order_below_the_minimum_notional_is_rejected_not_filled():
    spec = InstrumentSpec("INJUSDT", tick_size=0.001, qty_step=0.1, min_qty=0.1,
                          min_notional=100.0)
    sim = ExecutionSimulator(spec, latency=LatencyModel())
    sim.on_book(_book(1000))
    order = sim.submit(BUY, 0.1, kind=LIMIT, price=5.750, decided_at_ms=1000)
    assert order.state == REJECTED and "min_notional" in order.reject_reason


def test_a_quantity_that_rounds_to_zero_is_rejected():
    sim = _sim()
    order = sim.submit(BUY, 0.04, kind=LIMIT, price=5.750, decided_at_ms=1000)
    assert order.state == REJECTED and "zero" in order.reject_reason


# ---- fees ---------------------------------------------------------------

def test_maker_and_taker_fills_are_charged_their_own_fee():
    sim = _sim(fees=FeeSchedule(maker_bps=2.0, taker_bps=5.5))
    taker = sim.submit(BUY, 10.0, kind=MARKET, decided_at_ms=1000)
    sim.on_book(_book(1200))
    assert taker.fills[0].liquidity == TAKER
    assert taker.fills[0].fee == pytest.approx(taker.fills[0].notional * 5.5 / 10_000)

    maker = sim.submit(BUY, 10.0, kind=LIMIT, price=5.749, decided_at_ms=1300)
    sim.on_book(_book(1500, bids=[(5.749, 0.0)], asks=[(5.751, 80.0)]))
    sim.on_trade(Trade(exchange_ms=1600, recv_ms=1600, price=5.740, size=1, side=SELL))
    assert maker.fills[0].liquidity == MAKER
    assert maker.fills[0].fee == pytest.approx(maker.fills[0].notional * 2.0 / 10_000)


def test_no_rebate_is_ever_assumed():
    """A negative maker fee is the easiest way to manufacture a
    profitable market-making backtest."""
    assert FeeSchedule().maker_bps > 0
    assert FeeSchedule().assumed is True


# ---- ttl ----------------------------------------------------------------

def test_an_order_past_its_ttl_is_cancelled_and_the_cancel_still_takes_time():
    sim = _sim()
    order = sim.submit(BUY, 10.0, kind=LIMIT, price=5.750, decided_at_ms=1000,
                       ttl_ms=500)
    sim.on_book(_book(1200))                         # acked at 1150
    assert order.state == OPEN
    sim.on_book(_book(1700))                         # ttl reached: cancel asked
    assert order.state == OPEN and order.cancel_requested_at_ms == 1700
    sim.on_book(_book(1850))                         # cancel lands
    assert order.state == CANCELLED


def test_no_usable_book_at_arrival_rejects_rather_than_guesses():
    sim = ExecutionSimulator(SPEC, latency=LatencyModel())
    sim.on_book(BookState(exchange_ms=1000, recv_ms=1000, bids=[], asks=[]))
    order = sim.submit(BUY, 10.0, kind=MARKET, decided_at_ms=1000)
    sim.on_book(BookState(exchange_ms=2000, recv_ms=2000, bids=[], asks=[]))
    assert order.state == REJECTED and "no usable book" in order.reject_reason
