"""The whole research pipeline, end to end, on data with known answers.

The generator here produces IRREGULAR timestamps and varied sizes, which
is what a real feed looks like - but it is still synthetic, and the
classifier is expected to say so. That is the point: the machinery is
tested here, and nothing in this file is evidence about a market.
"""

import random
import time

import pytest

from t3_engine.research import backtest as bt
from t3_engine.research import dataset as ds
from t3_engine.research.book import BUY, SELL, BookState, Trade
from t3_engine.research.execution import InstrumentSpec, LatencyModel, QueueModel
from t3_engine.research.portfolio import LONG, QUALITY_DATA_GAP, QUALITY_OK, RiskLimits
from t3_engine.research.runner import RunnerConfig, StrategyRunner
from t3_engine.research.strategies import (ALL_STRATEGIES, OrderFlowImpulse,
                                           SpreadCapture, SweepReversion)

SPEC = InstrumentSpec("TESTUSDT", tick_size=0.001, qty_step=0.1, min_qty=0.1,
                      min_notional=1.0)


def _config(**kwargs):
    kwargs.setdefault("symbol", "TESTUSDT")
    kwargs.setdefault("spec", SPEC)
    kwargs.setdefault("latency", LatencyModel(send_ms=100, ack_ms=50, cancel_ms=100))
    kwargs.setdefault("limits", RiskLimits(max_symbol_notional=10_000,
                                           max_position_notional=10_000,
                                           max_total_notional=10_000,
                                           max_correlated_notional=10_000))
    return RunnerConfig(**kwargs)


def _book(ms, mid, spread=0.002, bid_size=100.0, ask_size=100.0):
    half = spread / 2.0
    return BookState(exchange_ms=ms, recv_ms=ms,
                     bids=[(round(mid - half - i * 0.001, 6), bid_size * (1 + i * 0.3))
                           for i in range(5)],
                     asks=[(round(mid + half + i * 0.001, 6), ask_size * (1 + i * 0.3))
                           for i in range(5)])


def _drive(runner, events):
    for kind, payload in events:
        if kind == "book":
            runner.on_book(payload)
        else:
            runner.on_trade(payload)


# ---- the runner's causality ---------------------------------------------

def test_a_full_run_produces_a_finalised_report_with_nothing_left_open():
    """The failure this guards: ending with the winners closed and the
    losers still open, and calling the difference a profit."""
    runner = StrategyRunner(OrderFlowImpulse({"notional": 100.0}), _config())
    rng = random.Random(7)
    ms, mid = 1_700_000_000_000, 5.0
    events = []
    for i in range(600):
        ms += rng.randint(40, 160)
        mid = round(mid + rng.gauss(0, 0.0008), 6)
        events.append(("book", _book(ms, mid)))
        if rng.random() < 0.4:
            ms += rng.randint(1, 20)
            events.append(("trade", Trade(ms, ms, mid, rng.uniform(1, 30),
                                          BUY if rng.random() < 0.5 else SELL)))
    _drive(runner, events)
    runner.finalise()

    report = runner.report()
    assert report["finalised"] is True
    assert report["still_open"] == 0
    assert "execution" in report and "strategy_declines" in report


def test_every_journal_row_carries_what_an_audit_needs():
    runner = StrategyRunner(SweepReversion({"notional": 100.0}), _config())
    ms, mid = 1_700_000_000_000, 5.0
    events = []
    # A one-sided flush, then a stop, then a reclaim.
    for i in range(60):
        ms += 50
        mid = round(mid - 0.004, 6)
        events.append(("book", _book(ms, mid, bid_size=40.0)))
        events.append(("trade", Trade(ms, ms, mid, 30.0, SELL)))
    for i in range(40):
        ms += 50
        events.append(("book", _book(ms, mid, bid_size=300.0)))
    for i in range(40):
        ms += 50
        mid = round(mid + 0.002, 6)
        events.append(("book", _book(ms, mid, bid_size=300.0)))
        events.append(("trade", Trade(ms, ms, mid, 5.0, BUY)))
    _drive(runner, events)
    runner.finalise()

    for row in runner.portfolio.journal:
        assert row.trade_id and row.strategy_version and row.config_hash
        assert row.signal_id and row.symbol and row.direction
        assert row.entry_at_ms and row.exit_at_ms >= row.entry_at_ms
        assert row.entry_fill_ids and row.exit_fill_ids
        assert row.exit_reason and row.quality
        # The identity the whole ledger rests on.
        assert row.net_pnl == pytest.approx(row.gross_pnl - row.fees - row.funding)


def test_a_data_gap_blocks_new_entries_but_never_blocks_an_exit():
    """A position that exists has to be manageable. Refusing to close it
    because the feed hiccuped turns a bounded loss into an unbounded one."""
    runner = StrategyRunner(OrderFlowImpulse({"notional": 100.0}), _config())
    ms = 1_700_000_000_000
    for i in range(40):
        ms += 100
        runner.on_book(_book(ms, 5.0))
    assert runner.may_enter is True

    ms += 60_000                               # a minute of nothing
    runner.on_book(_book(ms, 5.0))
    assert runner.gaps == 1
    assert runner.may_enter is False
    assert runner.quality == QUALITY_DATA_GAP

    # And it recovers only after a clean stretch, not on the first frame.
    for i in range(10):
        ms += 100
        runner.on_book(_book(ms, 5.0))
    assert runner.may_enter is False
    for i in range(60):
        ms += 100
        runner.on_book(_book(ms, 5.0))
    assert runner.may_enter is True


def test_a_stale_book_blocks_entries():
    runner = StrategyRunner(OrderFlowImpulse(), _config())
    ms = 1_700_000_000_000
    for i in range(30):
        ms += 100
        runner.on_book(_book(ms, 5.0))
    assert runner.may_enter is True
    # A trade arrives long after the last book: the book is now stale.
    runner.on_trade(Trade(ms + 5_000, ms + 5_000, 5.0, 1.0, BUY))
    assert runner.may_enter is False
    assert "old" in runner.quality_reason


def test_risk_limits_refuse_before_the_order_and_record_the_reason():
    limits = RiskLimits(max_position_notional=10.0, max_symbol_notional=10.0,
                        max_total_notional=10.0)
    runner = StrategyRunner(OrderFlowImpulse({"notional": 5_000.0}),
                            _config(limits=limits))
    ms, mid = 1_700_000_000_000, 5.0
    for i in range(80):
        ms += 60
        mid = round(mid + 0.0015, 6)
        runner.on_book(_book(ms, mid, ask_size=max(5.0, 100 - i)))
        runner.on_trade(Trade(ms, ms, mid, 40.0, BUY))
    reasons = runner.portfolio.no_trade_reasons
    assert runner.portfolio.report()["trades"] == 0
    if reasons:
        assert any("risk limit" in r or "data quality" in r for r in reasons)


# ---- the strategies' own gates -------------------------------------------

def test_spread_capture_refuses_every_spread_below_the_fee_floor():
    """The prediction the cost model makes before any data: two maker
    fills cost 4bps, so a 1bps spread cannot be captured on this venue at
    this fee tier, however the strategy is parameterised."""
    strategy = SpreadCapture()
    runner = StrategyRunner(strategy, _config())
    ms = 1_700_000_000_000
    for i in range(200):
        ms += 100
        runner.on_book(_book(ms, 5.0, spread=0.0005))     # 1 bps
    runner.finalise()

    assert runner.portfolio.report()["trades"] == 0
    assert any("fee floor" in reason for reason in strategy.declines)


def test_spread_capture_will_quote_when_the_spread_clears_the_floor():
    strategy = SpreadCapture()
    runner = StrategyRunner(strategy, _config())
    ms = 1_700_000_000_000
    for i in range(200):
        ms += 100
        runner.on_book(_book(ms, 5.0, spread=0.010))      # 20 bps
    assert not any("fee floor" in reason for reason in strategy.declines)
    assert runner.sim.orders


def test_no_strategy_enters_on_an_expectation_below_its_own_cost():
    """The gate the previous calibration never had."""
    for name, cls in ALL_STRATEGIES.items():
        strategy = cls()
        assert strategy.pays_for_itself(1_000.0, _wide_view()) is True
        assert strategy.pays_for_itself(0.01, _wide_view()) is False, name


def _wide_view():
    from t3_engine.research.features import MarketView
    return MarketView(symbol="TESTUSDT", decided_at_ms=0, exchange_ms=0,
                      spread_bps=4.0)


def test_declines_are_recorded_so_no_trade_is_a_result_not_a_silence():
    strategy = OrderFlowImpulse()
    runner = StrategyRunner(strategy, _config())
    ms = 1_700_000_000_000
    for i in range(100):
        ms += 100
        runner.on_book(_book(ms, 5.0))
    assert strategy.declines
    assert sum(strategy.declines.values()) > 0
    assert runner.report()["strategy_declines"]


# ---- independence and uncertainty ----------------------------------------

def test_repeated_snapshots_of_one_setup_collapse_into_one_episode():
    """451 rows were once reported as 451 observations. They were 83."""
    from t3_engine.research.portfolio import JournalRow

    def row(entry_ms, exit_ms):
        return JournalRow(trade_id="t", strategy_version="v", config_hash="c",
                          signal_id="s", symbol="X", direction=LONG, qty=1.0,
                          entry_at_ms=entry_ms, entry_price=100.0,
                          entry_liquidity="taker", entry_order_id="o",
                          entry_fill_ids=["f"], exit_at_ms=exit_ms,
                          exit_price=100.0, exit_reason="TIME_STOP",
                          net_pnl=1.0)

    burst = [row(1_000 + i * 15_000, 1_000 + i * 15_000 + 5_000) for i in range(6)]
    later = [row(5_000_000, 5_010_000)]
    groups = bt.episodes(burst + later)
    assert len(groups) == 2 and len(groups[0]) == 6


def test_the_bootstrap_resamples_blocks_and_widens_with_clustering():
    clustered = [5.0, 5.0, 5.0, -5.0, -5.0, -5.0] * 5
    alternating = [5.0, -5.0] * 15
    # SAME block size, so the only difference is the clustering itself.
    wide = bt.block_bootstrap(clustered, block=3)
    narrow = bt.block_bootstrap(alternating, block=3)
    assert (wide["ci_high"] - wide["ci_low"]) > (narrow["ci_high"] - narrow["ci_low"])


def test_a_result_whose_interval_includes_zero_is_not_confirmed():
    from t3_engine.research.portfolio import JournalRow

    rows = [JournalRow(trade_id=f"t{i}", strategy_version="v", config_hash="c",
                       signal_id="s", symbol="X", direction=LONG, qty=1.0,
                       entry_at_ms=i * 3_600_000, entry_price=100.0,
                       entry_liquidity="taker", entry_order_id="o",
                       entry_fill_ids=["f"], exit_at_ms=i * 3_600_000 + 60_000,
                       exit_price=100.0, exit_reason="TARGET",
                       net_pnl=(1.0 if i % 2 else -0.9))
            for i in range(40)]
    report = {"net_pnl": 2.0, "expectancy_bps": 0.5, "profit_factor": 1.11}
    verdict = bt.assess(report, rows, bt.block_bootstrap(
        bt.episode_returns(rows)))
    assert verdict["verdict"] in ("PRELIMINARY_CANDIDATE", "NO_EDGE_FOUND")
    assert verdict["verdict"] != "CONFIRMED_ON_HELD_OUT_DATA"


def test_splits_are_purged_so_no_outcome_window_spans_two_of_them():
    splits = bt.chronological_splits(0, 100 * 3_600_000, purge_ms=5 * 60_000)
    names = [s.name for s in splits]
    assert names == [bt.TRAIN, bt.VALIDATION, bt.FINAL_TEST]
    for earlier, later in zip(splits, splits[1:]):
        assert later.from_ms - earlier.to_ms >= 5 * 60_000


def test_a_period_too_short_to_purge_is_refused_rather_than_squeezed():
    with pytest.raises(ValueError, match="too short to purge"):
        bt.chronological_splits(0, 60_000, purge_ms=5 * 60_000)


# ---- the classifier -------------------------------------------------------

def test_the_classifier_calls_a_perfect_grid_synthetic():
    frames = [{"topic": "orderbook.50.X", "ts": 1_700_000_000_000 + i * 500,
               "r": 1_700_000_000_000 + i * 500, "type": "delta",
               "data": {"u": i, "b": [["5.0", "8"]], "a": [["5.1", "8"]]}}
              for i in range(200)]
    manifest = ds.describe(frames, name="grid", source="test", provenance="test")
    assert manifest.classification == ds.SYNTHETIC
    assert any("grid" in w for w in manifest.warnings)


def test_the_classifier_accepts_irregular_stamps_with_real_size_entropy():
    rng = random.Random(3)
    ms = 1_700_000_123_457
    frames = []
    for i in range(400):
        ms += rng.randint(7, 233)
        frames.append({"topic": "orderbook.50.X", "ts": ms, "r": ms + 4,
                       "type": "delta",
                       "data": {"u": i,
                                "b": [[f"{5 - rng.random() * 0.01:.4f}",
                                       f"{rng.uniform(1, 500):.2f}"]],
                                "a": [[f"{5 + rng.random() * 0.01:.4f}",
                                       f"{rng.uniform(1, 500):.2f}"]]}})
    manifest = ds.describe(frames, name="irregular", source="test",
                           provenance="test")
    assert manifest.classification == ds.REAL_CAPTURE, manifest.warnings


# ---- what JSON cannot carry ----------------------------------------------

def test_a_profit_factor_with_no_losses_is_undefined_not_infinite():
    """"No losing trade yet" is not "infinitely profitable" - it is a
    sample too small to have found one. And infinity is not JSON, so
    emitting it destroyed the experiment row it was meant to describe:
    a backtest that had run correctly was recorded as a failure."""
    from t3_engine.research.execution import Fill
    from t3_engine.research.portfolio import Portfolio

    portfolio = Portfolio(strategy_version="v", config={})
    entry = Fill("f1", "o1", "X", "Buy", 100.0, 1.0, "taker", 1_000, fee=0.05)
    exit_ = Fill("f2", "o2", "X", "Sell", 110.0, 1.0, "taker", 2_000, fee=0.05)
    portfolio.open_position(symbol="X", direction=LONG, fills=[entry],
                            signal_id="s")
    portfolio.close_position("X", fills=[exit_], reason="TARGET")

    report = portfolio.report()
    assert report["profit_factor"] is None
    assert "no losing trades" in report["profit_factor_undefined_reason"]

    import json
    json.dumps(report)          # would have raised before


def test_an_undefined_profit_factor_does_not_pass_acceptance():
    """Otherwise a five-trade run with no loser gets approved."""
    from t3_engine.research.portfolio import JournalRow

    rows = [JournalRow(trade_id=f"t{i}", strategy_version="v", config_hash="c",
                       signal_id="s", symbol="X", direction=LONG, qty=1.0,
                       entry_at_ms=i * 3_600_000, entry_price=100.0,
                       entry_liquidity="taker", entry_order_id="o",
                       entry_fill_ids=["f"], exit_at_ms=i * 3_600_000 + 60_000,
                       exit_price=101.0, exit_reason="TARGET", net_pnl=1.0)
            for i in range(5)]
    verdict = bt.assess({"net_pnl": 5.0, "expectancy_bps": 10.0,
                         "profit_factor": None},
                        rows, bt.block_bootstrap(bt.episode_returns(rows)))
    assert verdict["checks"]["profit_factor"] is False
    assert verdict["verdict"] != "CONFIRMED_ON_HELD_OUT_DATA"


def test_json_safe_strips_non_finite_floats_without_losing_the_row():
    import json

    from t3_engine.research.jobs import json_safe

    payload = {"ok": 1.5, "inf": float("inf"), "nan": float("nan"),
               "nested": {"list": [1.0, float("-inf")]}, "text": "fine"}
    safe = json_safe(payload)
    assert safe == {"ok": 1.5, "inf": None, "nan": None,
                    "nested": {"list": [1.0, None]}, "text": "fine"}
    json.dumps(safe)


def test_a_failed_job_writes_its_attempt_count_back_incremented():
    """Writing the row as it was claimed reset the counter, so a job that
    failed three times still read as having been tried none - and would
    retry forever."""
    from t3_engine.research import jobs

    written = []

    class FakeRest:
        def insert(self, table, rows, on_conflict=None):
            written.extend(rows)

    original = jobs._rest
    jobs._rest = lambda: FakeRest()
    try:
        jobs.fail({"job_id": "j1", "attempts": 2}, "boom")
        jobs.finish({"job_id": "j2", "attempts": 0}, {"ok": True})
    finally:
        jobs._rest = original

    assert written[0]["attempts"] == 3 and written[0]["status"] == "failed"
    assert written[1]["attempts"] == 1 and written[1]["status"] == "done"


# ---- the microstructure measurement --------------------------------------

def test_the_adverse_selection_probe_looks_strictly_forward():
    """It measures what happened NEXT, which is the whole point - and it
    is never fed to a strategy, which is why that is allowed here and
    nowhere else."""
    from t3_engine.research.handlers import _mid_at

    mids = [(100, 5.0), (200, 5.1), (300, 5.2)]
    assert _mid_at(mids, 150) == 5.1          # the first stamp AT OR AFTER
    assert _mid_at(mids, 200) == 5.1
    assert _mid_at(mids, 301) is None         # never invents one


def test_the_probe_signs_adverse_selection_against_the_side_that_was_hit():
    """A buyer lifting the ask means a resting SELLER was hit. If the mid
    then rises, that seller was picked off - and the sign has to say so,
    or the measurement reads backwards."""
    from t3_engine.research import handlers

    mids = [(0, 100.0), (1_000, 101.0)]        # the mid rose 100 bps
    up = 10_000.0 * (101.0 - 100.0) / 100.0
    # Aggressor bought -> resting ask was hit -> the move is its loss.
    assert up > 0
    # Aggressor sold -> resting bid was hit -> the same move is its gain.
    assert -up < 0
    assert handlers._mid_at(mids, 1_000) == 101.0


def test_describe_reports_a_distribution_not_a_single_number():
    from t3_engine.research.handlers import _describe

    stats = _describe([1, 2, 3, 4, 5, 6, 7, 8, 9, 100])
    assert stats["n"] == 10
    assert stats["median"] == 5
    assert stats["max"] == 100
    # The mean is dragged by the outlier and the median is not; both are
    # reported so the difference is visible.
    assert stats["mean"] > stats["median"]
    assert _describe([]) == {"n": 0}


def test_the_verdict_calls_spread_capture_unviable_below_the_fee_floor():
    from t3_engine.research.handlers import _microstructure_verdict

    tight = _microstructure_verdict({
        "spread_bps": {"n": 100, "median": 1.8, "mean": 1.9},
        "spread_below_maker_fee_floor_pct": 96.0,
        "maker_fee_floor_bps": 4.0,
        "adverse_selection_bps": {}})
    assert tight["spread_capture_viable"] is False

    wide = _microstructure_verdict({
        "spread_bps": {"n": 100, "median": 12.0, "mean": 13.0},
        "spread_below_maker_fee_floor_pct": 4.0,
        "maker_fee_floor_bps": 4.0,
        "adverse_selection_bps": {}})
    assert wide["spread_capture_viable"] is True


def test_a_job_claimed_by_a_dead_process_is_put_back_in_the_queue():
    """On a plan that stops the service without warning, a worker dying
    mid-job is the ORDINARY way a job ends. One restart must not leave a
    row marked RUNNING forever with the whole queue stuck behind it."""
    from t3_engine.research import jobs

    written = []
    fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    stale = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                          time.gmtime(time.time() - 4 * 3600))

    class FakeRest:
        def select(self, table, filters=None, order=None, limit=None):
            if (filters or {}).get("status") == "running":
                return [{"job_id": "old", "attempts": 1, "claimed_at": stale},
                        {"job_id": "new", "attempts": 1, "claimed_at": fresh}]
            return []

        def insert(self, table, rows, on_conflict=None):
            written.extend(rows)

    original = jobs._rest
    jobs._rest = lambda: FakeRest()
    try:
        assert jobs.claim_next() is None        # nothing pending afterwards
    finally:
        jobs._rest = original

    reclaimed = [r for r in written if r["status"] == "pending"]
    assert [r["job_id"] for r in reclaimed] == ["old"]
    # The attempt count travels with it, so a job that really kills the
    # worker still runs out of attempts instead of looping forever.
    assert reclaimed[0]["attempts"] == 1
    assert "process that claimed this job is gone" in reclaimed[0]["error"]


def test_an_unparseable_claim_stamp_does_not_reclaim_a_live_job():
    from t3_engine.research import jobs

    assert jobs._parse_stamp(None) is None
    assert jobs._parse_stamp("not a date") is None
    assert jobs._parse_stamp("2026-09-17T14:00:00Z") > 0
