import dataclasses

import pytest

from t3_engine.backtest.engine import BacktestConfig, BacktestEngine
from t3_engine.backtest.metrics import compute_metrics, compute_metrics_by_wave
from t3_engine.backtest.synthetic_data import generate_synthetic_series
from t3_engine.common.models import Candle, Position
from t3_engine.common.types import Timeframe, TradeSide, WaveLabel


def test_backtest_never_opens_trades_on_a_non_tradeable_timeframe():
    """TRADEABLE_TIMEFRAMES is (M5, M15, H1, H4) - 1h/4h were added on the
    project owner's explicit instruction to enable trading there too, so
    1m/3m/seconds-level degrees are what remain confirmation-only now. The
    dashboard lets a user pick ANY timeframe just to view structure/wave
    counts (server.py's /api/run `timeframe` param), so this guard is what
    stops a confirmation-only pick from silently opening real paper trades
    too - it must never fire a signal at all here, not just skip opening a
    position from one."""
    candles = [dataclasses.replace(c, timeframe=Timeframe.M1) for c in generate_synthetic_series(num_cycles=2)]
    engine = BacktestEngine(BacktestConfig(symbol="TESTUSDT", entry_confidence_threshold=50.0, degree=Timeframe.M1))
    result = engine.run(candles)
    assert result["signals"] == []
    assert result["closed_positions"] == []
    assert result["open_positions"] == []
    # Structure detection itself is unaffected by the guard - it's ONLY
    # trade origination that's gated by timeframe.
    assert len(engine.pivot_detector.pivots) > 0


def test_backtest_engine_builds_subwave_history_for_motive_waves():
    """Spec follow-up: "waves and subwaves should be accounted for". Once
    a motive (1/3/5) wave joins the confirmed chain, _update_subwaves
    should have subdivided it into its own i-ii-iii-iv-v count over that
    wave's own candle range - the full end-to-end wiring, not just the
    isolated build_subwaves() unit."""
    candles = generate_synthetic_series(num_cycles=2)
    engine = BacktestEngine(BacktestConfig(symbol="TESTUSDT", entry_confidence_threshold=50.0))
    engine.run(candles)
    assert len(engine.scenario_engine.confirmed_chain) > 0  # sanity: the primary count did confirm waves
    for subwave in engine.subwave_history:
        assert subwave.parent_wave_id is not None


def test_subwaves_never_outlive_the_chain_wave_they_belong_to():
    """A subwave is only meaningful as a subdivision OF its parent. When a
    re-anchor truncates the confirmed chain, the subwaves under the waves
    it dropped must be pruned with them - otherwise they'd hang on the
    chart as orphaned i-ii-iii labels under a count that no longer
    exists, which is the same class of bug the chain replaced."""
    candles = generate_synthetic_series(num_cycles=2)
    engine = BacktestEngine(BacktestConfig(symbol="TESTUSDT", entry_confidence_threshold=50.0))
    engine.run(candles)

    chain_keys = {(w.label, w.start_timestamp) for w in engine.scenario_engine.confirmed_chain}
    assert set(engine.subwaves_by_parent).issubset(chain_keys)

    # Force a truncation: drop the chain entirely and re-run the pruning
    # pass - every subwave must go with it.
    engine.scenario_engine.confirmed_chain = []
    engine._update_subwaves(candles)
    assert engine.subwaves_by_parent == {}
    assert engine.subwave_history == []


def test_backtester_rejects_unclosed_candles():
    engine = BacktestEngine(BacktestConfig(symbol="TESTUSDT"))
    bad = Candle(Timeframe.M5, 0, 299999, 100, 101, 99, 100, closed=False)
    with pytest.raises(ValueError):
        engine.run([bad])


def test_synthetic_series_generates_valid_ohlc():
    candles = generate_synthetic_series(num_cycles=1)
    assert len(candles) > 50
    for c in candles:
        assert c.high >= max(c.open, c.close)
        assert c.low <= min(c.open, c.close)
        assert c.closed is True


def test_backtest_end_to_end_runs_and_produces_signals():
    candles = generate_synthetic_series(num_cycles=2)
    engine = BacktestEngine(BacktestConfig(symbol="TESTUSDT", entry_confidence_threshold=50.0))
    result = engine.run(candles)
    assert "closed_positions" in result
    assert isinstance(result["signals"], list)
    # with a realistic, valid impulse baked into the synthetic data, at
    # least the Elliott/Fibonacci machinery should have found *some*
    # candidate scenario and evaluated *some* entry over 2 full cycles.
    assert len(result["signals"]) > 0


def test_backtest_determinism_same_input_same_output():
    candles = generate_synthetic_series(num_cycles=1)
    engine1 = BacktestEngine(BacktestConfig(symbol="TESTUSDT", entry_confidence_threshold=50.0))
    result1 = engine1.run(candles)
    engine2 = BacktestEngine(BacktestConfig(symbol="TESTUSDT", entry_confidence_threshold=50.0))
    result2 = engine2.run(candles)
    assert len(result1["signals"]) == len(result2["signals"])
    for s1, s2 in zip(result1["signals"], result2["signals"]):
        assert s1.decision == s2.decision
        assert s1.confidence == s2.confidence
        assert s1.wave_label == s2.wave_label
        assert s1.stop_loss == s2.stop_loss


def test_backtest_no_lookahead_truncated_run_matches_prefix_of_full_run():
    """The core anti-repaint guarantee (section 16): decisions made while
    replaying candles[0:K] must be identical whether or not candles beyond
    K exist yet. We run the full series, then re-run only a prefix, and
    verify every signal the prefix run produced also appears - with
    identical business fields - at the same position in the full run."""
    candles = generate_synthetic_series(num_cycles=2)
    cut = len(candles) * 2 // 3

    full_engine = BacktestEngine(BacktestConfig(symbol="TESTUSDT", entry_confidence_threshold=50.0))
    full_result = full_engine.run(candles)

    prefix_engine = BacktestEngine(BacktestConfig(symbol="TESTUSDT", entry_confidence_threshold=50.0))
    prefix_result = prefix_engine.run(candles[:cut])

    assert len(prefix_result["signals"]) <= len(full_result["signals"])
    for s_prefix, s_full in zip(prefix_result["signals"], full_result["signals"]):
        assert s_prefix.decision == s_full.decision
        assert s_prefix.wave_label == s_full.wave_label
        assert s_prefix.confidence == s_full.confidence
        assert s_prefix.stop_loss == s_full.stop_loss
        assert s_prefix.data_available_at_signal == s_full.data_available_at_signal


# ---- metrics ----

def _pos(pnl, risk, mae=1.0, mfe=2.0, label=WaveLabel.W3, side=TradeSide.LONG):
    return Position(position_id="p", symbol="X", side=side, entry_price=100, quantity=1,
                     initial_quantity=1, stop_loss=95, take_profits=[], opened_at=0,
                     wave_label=label, signal_id="s", risk_amount=risk, realized_pnl=pnl,
                     closed=True, mae=mae, mfe=mfe)


def test_compute_metrics_basic():
    positions = [_pos(100, 50), _pos(-50, 50), _pos(150, 50)]
    m = compute_metrics(positions, starting_equity=10_000)
    assert m.trades == 3
    assert m.winrate == pytest.approx(2 / 3)
    assert m.profit_factor == pytest.approx((100 + 150) / 50)
    assert m.average_r == pytest.approx((2 + -1 + 3) / 3)


def test_compute_metrics_empty():
    m = compute_metrics([])
    assert m.trades == 0
    assert m.winrate == 0.0


def test_compute_metrics_by_wave_splits_groups():
    positions = [_pos(100, 50, label=WaveLabel.W3, side=TradeSide.LONG),
                 _pos(-20, 20, label=WaveLabel.W4, side=TradeSide.SHORT)]
    grouped = compute_metrics_by_wave(positions)
    assert "3_LONG" in grouped
    assert "4_SHORT" in grouped
    assert grouped["3_LONG"].trades == 1


# ---- the synthetic source has to be a different chart per timeframe ----
# It returned the same 5m series whatever was asked for, so a
# multi-timeframe run analysed one chart four times and reconciled it with
# itself. The model caught it first: it wrote "the supplied data repeats
# 5m" into its own verdict and refused to call a trend.

def test_each_timeframe_gets_its_own_series():
    from t3_engine.backtest.synthetic_data import generate_synthetic_series_for
    from t3_engine.common.types import Timeframe

    series = {tf: generate_synthetic_series_for(tf, num_cycles=2)
              for tf in (Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.H4)}
    for tf, candles in series.items():
        assert candles, f"{tf.value} produced nothing"
        assert all(c.timeframe == tf for c in candles)
        bar_seconds = (candles[0].close_time - candles[0].open_time + 1) // 1000
        assert bar_seconds == tf.seconds, f"{tf.value} bars are {bar_seconds}s"
    # ...and they are genuinely different charts, not the same closes
    closes = {tf: tuple(round(c.close, 4) for c in candles[:20])
              for tf, candles in series.items()}
    assert len(set(closes.values())) == len(closes)


def test_aggregation_is_ordinary_ohlcv_rollup():
    """A 4h bar is built from its 5m bars the way an exchange builds one -
    first open, highest high, lowest low, last close, summed volume."""
    from t3_engine.backtest.synthetic_data import aggregate_candles, generate_synthetic_series
    from t3_engine.common.types import Timeframe

    base = generate_synthetic_series(num_cycles=2)
    rolled = aggregate_candles(base, Timeframe.M15)
    assert rolled[0].open == base[0].open
    assert rolled[0].close == base[2].close
    assert rolled[0].high == max(c.high for c in base[:3])
    assert rolled[0].low == min(c.low for c in base[:3])
    assert rolled[0].volume == sum(c.volume for c in base[:3])


def test_a_trailing_partial_group_is_dropped_not_emitted_short():
    """A half-formed 4h bar presented as a closed one is the same lookahead
    the rest of this engine refuses."""
    from t3_engine.backtest.synthetic_data import aggregate_candles, generate_synthetic_series
    from t3_engine.common.types import Timeframe

    base = generate_synthetic_series(num_cycles=2)[:10]     # 3 full 15m bars + 1 spare
    rolled = aggregate_candles(base, Timeframe.M15)
    assert len(rolled) == 3


def test_aggregation_reads_the_bar_length_from_the_series_itself():
    """The bug that drew wave labels over an empty chart.

    `aggregate_candles` used to divide the target timeframe by the module's
    own `_BASE_SECONDS` (300, the synthetic generator's 5m). Handed a
    STORED 15m series it therefore wanted 48 bars in a 4h bucket, found the
    16 that are actually there, discarded every bucket as unfinished and
    returned nothing. The Claude tab rendered "0 bars" with the count drawn
    on top of it.

    Aggregating in one step and in two must also give the same bars: 15m is
    a divisor of 1h, so rolling 5m->15m->1h cannot differ from 5m->1h."""
    from t3_engine.backtest.synthetic_data import aggregate_candles, generate_alternating_series
    from t3_engine.common.types import Timeframe

    base = generate_alternating_series(num_cycles=4)
    m15 = aggregate_candles(base, Timeframe.M15)
    assert m15, "the 5m rollup itself must still work"

    two_step = aggregate_candles(m15, Timeframe.H1)
    one_step = aggregate_candles(base, Timeframe.H1)
    assert two_step, "a stored 15m series must roll up, not vanish"
    assert [c.open_time for c in two_step] == [c.open_time for c in one_step]
    assert [c.high for c in two_step] == [c.high for c in one_step]
    assert [c.low for c in two_step] == [c.low for c in one_step]
    assert aggregate_candles(m15, Timeframe.H4), "4h from 15m must not come back empty"


def test_aggregation_never_pretends_to_refine_a_coarse_series():
    """Asked for a FINER timeframe than the data, there is nothing to do: a
    1h bar cannot be split back into twelve 5m ones. Returning the series
    unchanged is honest; returning [] would blank the chart again."""
    from t3_engine.backtest.synthetic_data import aggregate_candles, generate_alternating_series
    from t3_engine.common.types import Timeframe

    h1 = aggregate_candles(generate_alternating_series(num_cycles=4), Timeframe.H1)
    assert [c.open_time for c in aggregate_candles(h1, Timeframe.M5)] == [c.open_time for c in h1]
    assert [c.open_time for c in aggregate_candles(h1, Timeframe.H1)] == [c.open_time for c in h1]


def test_a_long_series_oscillates_instead_of_running_away():
    """Cycles compound at about +53% each. The 96 needed for a 4h series
    took the old generator to 3e10 and the chart became a vertical line;
    alternating direction keeps the structure and loses the drift."""
    from t3_engine.backtest.synthetic_data import generate_alternating_series

    candles = generate_alternating_series(num_cycles=40)
    highest = max(c.high for c in candles)
    lowest = min(c.low for c in candles)
    assert highest / lowest < 4, f"range {lowest:.2f}..{highest:.2f} is a runaway, not a chart"
    # and each up/down pair returns to about where it began
    assert 0.8 < candles[-1].close / candles[0].open < 1.25


def test_a_mirrored_cycle_is_still_a_valid_impulse_pointing_down():
    """The reflection negates every price difference and scales them all by
    the same factor, so the RATIOS - and therefore the hard rules - survive."""
    from t3_engine.backtest.synthetic_data import (
        _mirror_cycle,
        generate_synthetic_impulse_cycle,
    )

    bull = generate_synthetic_impulse_cycle(start_price=100.0)
    bear = _mirror_cycle(bull, pivot=100.0)
    assert len(bear) == len(bull)
    assert bear[-1].close < bear[0].open          # it points down
    assert all(c.high >= c.low for c in bear)     # high and low swapped correctly
    # the shape is preserved: the biggest leg is in the same place
    bull_range = max(c.high for c in bull) - min(c.low for c in bull)
    bear_range = max(c.high for c in bear) - min(c.low for c in bear)
    assert abs(bull_range - bear_range) < 1e-6
