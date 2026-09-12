import pytest

from t3_engine.common.types import Direction, WaveLabel
from t3_engine.risk_engine.risk_manager import RiskManager, validate_stop_not_widened


def test_position_size_derived_from_risk_and_stop_distance():
    rm = RiskManager(initial_equity=10_000)
    qty = rm.position_size(WaveLabel.W3, entry_price=100, stop_price=95)
    # risk = 1% of 10000 = 100; stop distance = 5 -> qty = 20
    assert qty == pytest.approx(20.0)


def test_wave4_uses_smaller_risk_than_wave3():
    rm = RiskManager(initial_equity=10_000)
    assert rm.risk_amount_for(WaveLabel.W4) < rm.risk_amount_for(WaveLabel.W3)


def test_can_open_trade_blocks_after_max_concurrent():
    rm = RiskManager(initial_equity=10_000, max_concurrent_trades=1, max_correlated_exposure=1.0)
    ok, _ = rm.can_open_trade(WaveLabel.W3)
    assert ok
    rm.register_trade_opened("p1", WaveLabel.W3, rm.risk_amount_for(WaveLabel.W3))
    ok, reason = rm.can_open_trade(WaveLabel.W3)
    assert not ok
    assert "MAX_CONCURRENT_TRADES_REACHED" in reason


def test_can_open_trade_blocks_on_correlated_exposure():
    rm = RiskManager(initial_equity=10_000, max_concurrent_trades=10, max_correlated_exposure=0.01)
    rm.register_trade_opened("p1", WaveLabel.W3, rm.risk_amount_for(WaveLabel.W3))  # uses up 1%
    ok, reason = rm.can_open_trade(WaveLabel.W3)
    assert not ok
    assert "MAX_CORRELATED_EXPOSURE_EXCEEDED" in reason


def test_daily_drawdown_disables_trading():
    rm = RiskManager(initial_equity=10_000, max_daily_drawdown=0.03)
    rm.register_trade_opened("p1", WaveLabel.W3, 100)
    rm.register_trade_closed("p1", realized_pnl=-400)  # 4% loss > 3% daily limit
    assert rm.trading_enabled is False
    ok, reason = rm.can_open_trade(WaveLabel.W3)
    assert not ok
    assert "TRADING_DISABLED" in reason


def test_total_drawdown_disables_trading_even_across_days():
    rm = RiskManager(initial_equity=10_000, max_daily_drawdown=1.0, max_total_drawdown=0.10)
    rm.register_trade_opened("p1", WaveLabel.W3, 100)
    rm.register_trade_closed("p1", realized_pnl=500)  # equity -> 10500, new peak
    rm.start_new_day()
    rm.register_trade_opened("p2", WaveLabel.W3, 100)
    rm.register_trade_closed("p2", realized_pnl=-1100)  # equity 9400, dd from peak 10500 = 10.48%
    assert rm.trading_enabled is False


def test_manual_reset_required_to_resume():
    rm = RiskManager(initial_equity=10_000, max_daily_drawdown=0.01)
    rm.register_trade_opened("p1", WaveLabel.W3, 100)
    rm.register_trade_closed("p1", realized_pnl=-200)
    assert rm.trading_enabled is False
    rm.manual_reset()
    assert rm.trading_enabled is True


def test_stop_can_only_tighten_never_widen_long():
    assert validate_stop_not_widened(original_stop=95, proposed_stop=97, side_direction=Direction.UP)
    assert not validate_stop_not_widened(original_stop=95, proposed_stop=93, side_direction=Direction.UP)


def test_stop_can_only_tighten_never_widen_short():
    assert validate_stop_not_widened(original_stop=105, proposed_stop=103, side_direction=Direction.DOWN)
    assert not validate_stop_not_widened(original_stop=105, proposed_stop=107, side_direction=Direction.DOWN)
