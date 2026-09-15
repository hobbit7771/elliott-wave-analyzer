"""The virtual ledger, rule by rule.

Every test here pins one of the six rules in the module docstring of
`virtual_trades.py`, and each is written so that removing the rule makes
it fail - not so that it passes on the current implementation. The point
of a paper ledger is that it can be wrong in public; the point of these
tests is that it cannot be wrong in its own favour.
"""

from __future__ import annotations

import pytest

from t3_engine.lead_engine.virtual_trades import (
    ABANDONED, CLOSED, LedgerConfig, OPEN, PENDING, VirtualLedger,
)


def signal(state="PRE_BREAK_LONG", direction="long", changed_at=1000.0,
           confidence=0.8, price=100.0):
    """A signal dict shaped exactly like SignalSnapshot.as_dict()."""
    return {"symbol": "TESTUSDT", "state": state, "direction": direction,
            "confidence": confidence, "changed_at": changed_at,
            "break_probability": 70.0, "level": 101.0, "price": price}


def ledger(**overrides):
    base = dict(notional=1_000.0, taker_fee=0.001, slippage_bps=1.0,
                stop_pct=0.01, target_pct=0.02, max_hold_ms=60_000,
                max_fill_gap_ms=3_000, max_book_age_ms=1_000.0)
    base.update(overrides)
    return VirtualLedger("TESTUSDT", LedgerConfig(**base))


# ---- RULE 1: entry is never at the signal's own price ----

def test_a_book_from_before_the_signal_cannot_fill_it():
    """The book the signal was computed from is not a fill.

    Filling at it is lookahead of the purest kind: the decision and the
    execution would share an instant. The intent must still be pending
    afterwards - not filled, and not abandoned either."""
    book = ledger()
    book.on_signal(signal(changed_at=1000.0), now_ms=1_000_000)

    # Same millisecond as the signal. Not "after".
    book.on_book(99.0, 101.0, book_at_ms=1_000_000, received_at_ms=1_000_000)
    assert book.pending[0].status == PENDING
    assert book.pending[0].entry_price is None

    # And a book from before it, which can happen when a slow snapshot
    # computes over an older frame.
    book.on_book(99.0, 101.0, book_at_ms=999_500, received_at_ms=999_500)
    assert book.pending[0].status == PENDING


def test_the_next_book_strictly_after_the_signal_is_the_one_that_fills():
    book = ledger()
    book.on_signal(signal(changed_at=1000.0), now_ms=1_000_000)
    book.on_book(99.0, 101.0, book_at_ms=1_000_001, received_at_ms=1_000_001)

    assert not book.pending
    assert len(book.open) == 1
    trade = book.open[0]
    assert trade.status == OPEN
    assert trade.entry_at_ms == 1_000_001
    assert trade.entry_at_ms > trade.signal_at_ms
    assert trade.as_dict()["fill_delay_ms"] == 1


def test_ordering_uses_the_arrival_clock_not_the_exchange_stamp():
    """A clock offset must not decide whether a fill happens.

    The exchange stamp here is 5 seconds BEHIND this process's clock - a
    plausible offset, and exactly what a naive comparison would read as
    "this book predates the signal" (so: never fill) or, with the offset
    reversed, as a fill on a book the signal already saw. Arrival is what
    is comparable with the signal's own timestamp, so arrival is what
    decides; the exchange stamp is recorded and reported."""
    book = ledger()
    book.on_signal(signal(changed_at=1000.0), now_ms=1_000_000)
    book.on_book(99.0, 101.0, book_at_ms=995_000, received_at_ms=1_000_200)

    assert len(book.open) == 1
    trade = book.open[0]
    assert trade.entry_at_ms == 1_000_200        # ordering: arrival
    assert trade.entry_book_ms == 995_000        # the record: the exchange


# ---- RULE 2: entry crosses the spread ----

def test_a_long_pays_the_ask_and_a_short_hits_the_bid_plus_slippage():
    """Filling at the mid is a half-spread of free money per trade."""
    long_book = ledger()
    long_book.on_signal(signal(changed_at=1000.0), now_ms=1_000_000)
    long_book.on_book(99.0, 101.0, book_at_ms=1_000_001, received_at_ms=1_000_001)
    long_trade = long_book.open[0]
    assert long_trade.entry_reference == 101.0                  # the ask
    assert long_trade.entry_price > 101.0                       # plus slippage
    assert long_trade.entry_price == pytest.approx(101.0 * (1 + 1.0 / 10_000))

    short_book = ledger()
    short_book.on_signal(signal(state="PRE_BREAK_SHORT", direction="short",
                                changed_at=1000.0), now_ms=1_000_000)
    short_book.on_book(99.0, 101.0, book_at_ms=1_000_001, received_at_ms=1_000_001)
    short_trade = short_book.open[0]
    assert short_trade.entry_reference == 99.0                  # the bid
    assert short_trade.entry_price < 99.0
    assert short_trade.entry_price == pytest.approx(99.0 * (1 - 1.0 / 10_000))


def test_the_exit_crosses_back_so_a_round_trip_pays_the_spread_twice():
    """A long exits on the bid, not the mid. The number that proves it:
    a position opened and closed at an unchanged book is DOWN by the
    spread plus both fees, never flat."""
    book = ledger(stop_pct=0.5, target_pct=0.5, max_hold_ms=1)
    book.on_signal(signal(changed_at=1000.0), now_ms=1_000_000)
    book.on_book(99.0, 101.0, book_at_ms=1_000_001, received_at_ms=1_000_001)
    book.on_book(99.0, 101.0, book_at_ms=1_000_100, received_at_ms=1_000_100)

    trade = book.closed[-1]
    assert trade.exit_reason == "time"
    assert trade.exit_reference == 99.0                         # the bid
    assert trade.gross_pnl < 0                                  # the spread
    assert trade.net_pnl < trade.gross_pnl                      # and the fees


# ---- RULE 3: no fill invented inside a gap ----

def test_an_intent_whose_first_fresh_book_arrives_too_late_is_abandoned():
    """This is the rule the user asked for in as many words: if the
    stream breaks, the system does not invent an execution price inside
    the gap. The intent is recorded as ABANDONED - a trade that did not
    happen - rather than filled wherever the book resurfaced."""
    book = ledger(max_fill_gap_ms=3_000)
    book.on_signal(signal(changed_at=1000.0), now_ms=1_000_000)

    # Nothing for four seconds, then the book comes back a long way off.
    book.on_book(120.0, 122.0, book_at_ms=1_004_000, received_at_ms=1_004_000)

    assert not book.pending
    assert not book.open
    assert book.closed[-1].status == ABANDONED
    assert book.closed[-1].entry_price is None
    assert book.abandoned_gap == 1
    assert "not filled at a price nobody saw" in book.closed[-1].note
    # An abandoned intent is not a trade and must not reach the P&L.
    summary = book.summary(120.0, 122.0)
    assert summary["closed_trades"] == 0
    assert summary["abandoned"] == 1
    assert summary["net_pnl"] == 0


def test_an_exit_taken_across_a_gap_is_flagged_rather_than_quietly_booked():
    """The stop was crossed somewhere nobody observed. The ledger takes
    the exit - a position cannot be left open forever because the feed
    blinked - but marks it `gap_uncertain`, so the journal says how much
    to trust the price."""
    book = ledger(max_fill_gap_ms=3_000, stop_pct=0.01)
    book.on_signal(signal(changed_at=1000.0), now_ms=1_000_000)
    book.on_book(99.0, 101.0, book_at_ms=1_000_001, received_at_ms=1_000_001)
    assert book.open

    # Ten seconds of silence, then a print far through the stop.
    book.on_book(80.0, 80.5, book_at_ms=1_010_000, received_at_ms=1_010_000)

    trade = book.closed[-1]
    assert trade.status == CLOSED
    assert trade.exit_reason == "stop"
    assert trade.gap_uncertain is True
    assert "estimate, not an observation" in trade.note
    assert book.summary()["gap_uncertain_exits"] == 1


def test_a_continuously_observed_exit_is_not_flagged():
    """The mirror of the test above - otherwise `gap_uncertain` could be
    hard-coded True and both tests would still pass."""
    book = ledger(max_fill_gap_ms=3_000, stop_pct=0.01)
    book.on_signal(signal(changed_at=1000.0), now_ms=1_000_000)
    book.on_book(99.0, 101.0, book_at_ms=1_000_001, received_at_ms=1_000_001)
    for step in range(1, 6):                   # a book every 200ms
        book.on_book(99.0 - step * 0.05, 101.0 - step * 0.05,
                     book_at_ms=1_000_001 + step * 200,
                     received_at_ms=1_000_001 + step * 200)
    book.on_book(99.0, 99.2, book_at_ms=1_000_001 + 1_200,
                 received_at_ms=1_000_001 + 1_200)
    trade = book.closed[-1]
    assert trade.exit_reason == "stop"
    assert trade.gap_uncertain is False


# ---- RULE 4: exits are stop, target, or time ----

@pytest.mark.parametrize("bid,ask,expected", [
    # Entry is the ask plus slippage: 101.51. Stop 1% below is 100.495,
    # target 2% above is 103.54, and both are read off the BID because a
    # long exits by selling into it.
    (100.40, 100.50, "stop"),
    (103.60, 103.70, "target"),
])
def test_stop_and_target_both_close_the_position(bid, ask, expected):
    book = ledger(stop_pct=0.01, target_pct=0.02)
    book.on_signal(signal(changed_at=1000.0), now_ms=1_000_000)
    book.on_book(101.4, 101.5, book_at_ms=1_000_001, received_at_ms=1_000_001)
    entry = book.open[0].entry_price
    stop, target = book.open[0].stop, book.open[0].target
    assert stop < entry < target

    book.on_book(bid, ask, book_at_ms=1_000_500, received_at_ms=1_000_500)
    assert not book.open
    assert book.closed[-1].exit_reason == expected


def test_a_position_that_reaches_neither_is_closed_on_time():
    """A position with no deadline is a position that cannot lose."""
    book = ledger(max_hold_ms=5_000, stop_pct=0.9, target_pct=0.9)
    book.on_signal(signal(changed_at=1000.0), now_ms=1_000_000)
    book.on_book(99.0, 101.0, book_at_ms=1_000_001, received_at_ms=1_000_001)
    assert book.open[0].deadline_ms == 1_000_001 + 5_000

    book.on_book(99.0, 101.0, book_at_ms=1_004_000, received_at_ms=1_004_000)
    assert book.open                                      # not yet
    book.on_book(99.0, 101.0, book_at_ms=1_005_001, received_at_ms=1_005_001)
    assert not book.open
    assert book.closed[-1].exit_reason == "time"


# ---- RULE 5: fees both ways ----

def test_both_sides_pay_the_taker_fee_and_the_net_is_the_gross_minus_it():
    book = ledger(taker_fee=0.001, slippage_bps=0.0,
                  stop_pct=0.9, target_pct=0.9, max_hold_ms=1)
    book.on_signal(signal(changed_at=1000.0), now_ms=1_000_000)
    book.on_book(100.0, 100.0, book_at_ms=1_000_001, received_at_ms=1_000_001)
    book.on_book(110.0, 110.0, book_at_ms=1_000_100, received_at_ms=1_000_100)

    trade = book.closed[-1]
    assert trade.fee_entry == pytest.approx(1_000.0 * 0.001)
    assert trade.fee_exit == pytest.approx(110.0 * trade.quantity * 0.001)
    assert trade.fees == pytest.approx(trade.fee_entry + trade.fee_exit)
    assert trade.gross_pnl == pytest.approx(100.0)          # 10% on 1000
    assert trade.net_pnl == pytest.approx(trade.gross_pnl - trade.fees)
    assert trade.net_pnl < trade.gross_pnl


def test_the_journal_shows_the_fees_and_the_slippage_separately():
    """"Комиссии и проскальзывание будут вычитаться и отображаться в
    журнале" - deducted AND shown, which means both are their own field
    rather than a difference the reader has to reconstruct."""
    book = ledger(taker_fee=0.001, slippage_bps=5.0,
                  stop_pct=0.9, target_pct=0.9, max_hold_ms=1)
    book.on_signal(signal(changed_at=1000.0), now_ms=1_000_000)
    book.on_book(99.0, 101.0, book_at_ms=1_000_001, received_at_ms=1_000_001)
    book.on_book(99.0, 101.0, book_at_ms=1_000_100, received_at_ms=1_000_100)

    row = book.journal()[0]
    assert row["fee_entry"] > 0 and row["fee_exit"] > 0
    assert row["fees"] == pytest.approx(row["fee_entry"] + row["fee_exit"])
    assert row["entry_slippage"] == pytest.approx(101.0 * 5.0 / 10_000)
    assert row["exit_slippage"] == pytest.approx(99.0 * 5.0 / 10_000)
    assert row["entry_reference"] == 101.0 and row["exit_reference"] == 99.0


# ---- RULE 6: a stale book is not a price ----

def test_a_stale_book_fills_nothing():
    book = ledger(max_book_age_ms=1_000.0)
    book.on_signal(signal(changed_at=1000.0), now_ms=1_000_000)
    book.on_book(99.0, 101.0, book_at_ms=1_000_500, received_at_ms=1_000_500,
                 book_age_ms=2_500.0)
    assert book.pending[0].status == PENDING
    assert book.skipped_stale_book == 1

    book.on_book(99.0, 101.0, book_at_ms=1_000_600, received_at_ms=1_000_600,
                 book_age_ms=40.0)
    assert book.open


# ---- what may open a trade at all ----

@pytest.mark.parametrize("state", ["IDLE", "WATCH", "PRE_SIGNAL",
                                   "INVALIDATED", "DATA_FAILURE"])
def test_only_actionable_states_open_an_intent(state):
    book = ledger()
    assert book.on_signal(signal(state=state), now_ms=1_000_000) is None
    assert not book.pending


def test_holding_one_state_opens_one_intent_not_one_per_tick():
    """`changed_at` does not move while a state persists - see
    SignalMachine.update. A state held for a minute is one setup, and a
    ledger that opened eighty positions from it would be measuring the
    snapshot interval, not the signal."""
    book = ledger()
    first = book.on_signal(signal(changed_at=1000.0), now_ms=1_000_000)
    assert first is not None
    for _ in range(20):
        assert book.on_signal(signal(changed_at=1000.0), now_ms=1_000_000) is None
    assert len(book.pending) == 1


def test_a_second_setup_is_skipped_while_one_is_still_working():
    book = ledger(max_open=1)
    book.on_signal(signal(changed_at=1000.0), now_ms=1_000_000)
    book.on_book(99.0, 101.0, book_at_ms=1_000_001, received_at_ms=1_000_001)
    assert book.on_signal(signal(changed_at=1010.0), now_ms=1_010_000) is None
    assert book.skipped_already_open == 1
    assert len(book.open) == 1


# ---- reporting ----

def test_open_pnl_is_marked_at_the_closing_touch_not_the_mid():
    """An open position valued at the mid carries an unbooked half-spread
    of profit, and a journal that does that reports an edge it does not
    have."""
    book = ledger(taker_fee=0.0, slippage_bps=0.0,
                  stop_pct=0.9, target_pct=0.9)
    book.on_signal(signal(changed_at=1000.0), now_ms=1_000_000)
    book.on_book(100.0, 100.0, book_at_ms=1_000_001, received_at_ms=1_000_001)
    quantity = book.open[0].quantity

    at_touch = book.open_pnl(109.0, 111.0)
    at_mid = (110.0 - 100.0) * quantity
    assert at_touch == pytest.approx((109.0 - 100.0) * quantity)
    assert at_touch < at_mid


def test_the_summary_separates_gross_fees_and_net():
    book = ledger(taker_fee=0.001, slippage_bps=0.0,
                  stop_pct=0.9, target_pct=0.9, max_hold_ms=1)
    for index in range(3):
        base = 1_000_000 + index * 10_000
        book.on_signal(signal(changed_at=base / 1000.0), now_ms=base)
        book.on_book(100.0, 100.0, book_at_ms=base + 1, received_at_ms=base + 1)
        book.on_book(101.0, 101.0, book_at_ms=base + 100, received_at_ms=base + 100)

    summary = book.summary()
    assert summary["closed_trades"] == 3
    assert summary["wins"] + summary["losses"] == 3
    assert summary["net_pnl"] == pytest.approx(summary["gross_pnl"]
                                               - summary["fees_paid"])
    assert summary["fees_vs_gross_pct"] > 0
    assert summary["exits_by_reason"]["time"] == 3


def test_the_journal_is_newest_first_and_carries_every_status():
    book = ledger(max_open=5, max_hold_ms=1, stop_pct=0.9, target_pct=0.9)
    book.on_signal(signal(changed_at=1000.0), now_ms=1_000_000)
    book.on_book(100.0, 100.0, book_at_ms=1_000_001, received_at_ms=1_000_001)
    book.on_book(100.0, 100.0, book_at_ms=1_000_100, received_at_ms=1_000_100)
    book.on_signal(signal(changed_at=1020.0), now_ms=1_020_000)
    book.on_book(100.0, 100.0, book_at_ms=1_020_001, received_at_ms=1_020_001)
    book.on_signal(signal(changed_at=1040.0), now_ms=1_040_000)

    rows = book.journal()
    assert [r["signal_at_ms"] for r in rows] == sorted(
        [r["signal_at_ms"] for r in rows], reverse=True)
    assert {r["status"] for r in rows} == {CLOSED, OPEN, PENDING}


def test_the_journal_is_bounded():
    book = ledger(journal_limit=5, max_hold_ms=1, stop_pct=0.9, target_pct=0.9)
    for index in range(30):
        base = 1_000_000 + index * 10_000
        book.on_signal(signal(changed_at=base / 1000.0), now_ms=base)
        book.on_book(100.0, 100.0, book_at_ms=base + 1, received_at_ms=base + 1)
        book.on_book(100.0, 100.0, book_at_ms=base + 100, received_at_ms=base + 100)
    assert len(book.closed) == 5


# ---- the wiring, which is what makes rule 1 structural ----

def test_the_engine_feeds_the_ledger_only_from_the_ingest_path():
    """SymbolState.on_orderbook is the only caller of on_book, and
    _compute_snapshot is the only caller of on_signal. That ordering -
    not a comparison inside the ledger - is what makes "the next fresh
    book after the signal" true: when the snapshot runs, every book it
    saw has already been offered to the ledger and rejected as not-after.

    Asserted on the source because it is a claim about call sites."""
    from pathlib import Path
    source = Path("t3_engine/lead_engine/state.py").read_text()
    assert source.count("self.ledger.on_book(") == 1
    assert source.count("self.ledger.on_signal(") == 1
    book_at = source.index("self.ledger.on_book(")
    signal_at = source.index("self.ledger.on_signal(")
    assert source.index("def on_orderbook") < book_at < source.index("def on_trade")
    assert source.index("def _compute_snapshot") < signal_at


def test_the_ledger_holds_no_execution_path():
    """Paper only, structurally: no credential, no exchange client, no
    order verb anywhere in the module."""
    from pathlib import Path
    source = Path("t3_engine/lead_engine/virtual_trades.py").read_text().lower()
    for forbidden in ("api_key", "secret", "requests", "httpx", "websocket",
                      "place_order", "create_order", "submit"):
        assert forbidden not in source, forbidden


# ---- end to end, through the real engine ------------------------------

def _engine_with_a_book():
    """A LeadEngine carrying one synced book, fed the way the stream
    feeds it - through handle_message, not by poking at the state."""
    import time as _time
    from t3_engine.lead_engine.config import LeadEngineConfig
    from t3_engine.lead_engine.engine import LeadEngine

    engine = LeadEngine(LeadEngineConfig(enabled=True, symbols=["INJUSDT"]))
    now = int(_time.time() * 1000)
    engine.handle_message("orderbook.50.INJUSDT", {
        "topic": "orderbook.50.INJUSDT", "type": "snapshot", "ts": now,
        "_received_at_ms": now,
        "data": {"u": 1,
                 "b": [[f"{5.70 - i * 0.01:.2f}", "100"] for i in range(20)],
                 "a": [[f"{5.71 + i * 0.01:.2f}", "100"] for i in range(20)]}})
    return engine, engine.states["INJUSDT"], now


def test_every_frame_carries_the_ledger():
    engine, state, _ = _engine_with_a_book()
    frame = engine.get_state("INJUSDT", force=True)
    ledger = frame["virtual_trades"]
    assert ledger["symbol"] == "INJUSDT"
    assert ledger["open_positions"] == 0
    assert ledger["net_pnl"] == 0
    assert ledger["recent"] == []
    # The costs it is charging are stated in the frame, not implied.
    assert ledger["config"]["taker_fee"] > 0
    assert ledger["config"]["max_fill_gap_ms"] > 0


def test_the_endpoint_reports_and_cannot_trade():
    engine, state, now = _engine_with_a_book()
    payload = engine.get_virtual_trades("INJUSDT")
    assert payload["tracked"] is True
    assert payload["summary"]["symbol"] == "INJUSDT"
    assert payload["journal"] == []

    # Reading it fifty times must not conjure a position out of a stream
    # that produced no signal.
    for _ in range(50):
        engine.get_virtual_trades("INJUSDT")
    assert state.ledger.summary()["open_positions"] == 0
    assert state.ledger.summary()["closed_trades"] == 0


def test_a_signal_fills_on_a_later_book_and_never_on_an_earlier_one():
    """The whole rule, exercised through the real ingest path.

    An intent is opened by hand at the snapshot's own moment - the signal
    machine will not produce an actionable state from a synthetic book -
    and the books that follow come in through handle_message exactly as
    the stream delivers them."""
    import time as _time
    engine, state, now = _engine_with_a_book()
    engine.get_state("INJUSDT", force=True)

    at = int(_time.time() * 1000)
    trade = state.ledger.on_signal(signal(changed_at=at / 1000.0), now_ms=at)
    assert trade is not None and trade.status == PENDING

    # A book whose ARRIVAL predates the signal cannot fill it, however
    # the exchange stamped it.
    engine.handle_message("orderbook.50.INJUSDT", {
        "topic": "orderbook.50.INJUSDT", "type": "delta", "ts": at + 5_000,
        "_received_at_ms": at - 50,
        "data": {"u": 2, "seq": 2, "b": [["5.70", "90"]], "a": []}})
    assert state.ledger.pending and not state.ledger.open

    # The next one, arriving after it, does.
    engine.handle_message("orderbook.50.INJUSDT", {
        "topic": "orderbook.50.INJUSDT", "type": "delta", "ts": at + 60,
        "_received_at_ms": at + 60,
        "data": {"u": 3, "seq": 3, "b": [["5.70", "80"]], "a": []}})
    assert not state.ledger.pending
    assert len(state.ledger.open) == 1
    filled = state.ledger.open[0]
    assert filled.entry_at_ms > filled.signal_at_ms
    assert filled.entry_price == pytest.approx(5.71 * (1 + 1.0 / 10_000), rel=1e-6)

    frame = engine.get_state("INJUSDT", force=True)
    assert frame["virtual_trades"]["open_positions"] == 1
    assert frame["virtual_trades"]["recent"][0]["status"] == OPEN
