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
    a motive (1/3/5) wave is archived into ScenarioEngine.wave_history,
    BacktestEngine._update_subwaves should have subdivided it into its own
    i-ii-iii-iv-v count over that wave's own candle range - the full
    end-to-end wiring, not just the isolated build_subwaves() unit."""
    candles = generate_synthetic_series(num_cycles=2)
    engine = BacktestEngine(BacktestConfig(symbol="TESTUSDT", entry_confidence_threshold=50.0))
    engine.run(candles)
    assert len(engine.scenario_engine.wave_history) > 0  # sanity: the primary count did confirm waves
    for (label, _start_ts), subwave in engine.subwave_history.items():
        assert label == subwave.label
        assert subwave.parent_wave_id is not None


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
