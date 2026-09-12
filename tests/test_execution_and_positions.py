import pytest

from t3_engine.common.models import Order, Scenario, TakeProfitLeg, next_id
from t3_engine.common.types import Direction, OrderSide, OrderStatus, Timeframe, TradeSide, WaveLabel, WaveStatus
from t3_engine.execution.binance_futures_live import build_order_params, sign_payload
from t3_engine.execution.paper import PaperExecutionEngine
from t3_engine.position_manager.manager import PositionManager, StopWideningRejected
from t3_engine.risk_engine.risk_manager import RiskManager


def make_signal():
    return type("S", (), {"signal_id": next_id("signal")})()


def test_paper_execution_applies_slippage_buy_vs_sell():
    engine = PaperExecutionEngine(slippage_bps=10.0, fee_bps=0.0)
    buy = Order(client_order_id="", symbol="BTCUSDT", side=OrderSide.BUY, quantity=1.0)
    sell = Order(client_order_id="", symbol="BTCUSDT", side=OrderSide.SELL, quantity=1.0)
    filled_buy = engine.submit_order(buy, reference_price=100.0)
    filled_sell = engine.submit_order(sell, reference_price=100.0)
    assert filled_buy.avg_fill_price > 100.0  # buys get worse (higher) price
    assert filled_sell.avg_fill_price < 100.0  # sells get worse (lower) price
    assert filled_buy.status == OrderStatus.FILLED


def test_paper_execution_duplicate_client_id_protection():
    engine = PaperExecutionEngine()
    order = Order(client_order_id="fixed-id", symbol="BTCUSDT", side=OrderSide.BUY, quantity=1.0)
    first = engine.submit_order(order, reference_price=100.0)
    order2 = Order(client_order_id="fixed-id", symbol="BTCUSDT", side=OrderSide.BUY, quantity=999.0)
    second = engine.submit_order(order2, reference_price=200.0)
    assert second is first  # returned the original, did not double-execute


def test_binance_signing_is_deterministic_and_offline():
    params = {"symbol": "BTCUSDT", "side": "BUY", "quantity": "1"}
    sig1 = sign_payload("secret", params)
    sig2 = sign_payload("secret", params)
    assert sig1 == sig2
    assert len(sig1) == 64  # hex sha256


def test_build_order_params_matches_binance_schema():
    order = Order(client_order_id="abc123", symbol="BTCUSDT", side=OrderSide.BUY, quantity=1.5, reduce_only=True)
    params = build_order_params(order)
    assert params["symbol"] == "BTCUSDT"
    assert params["side"] == "BUY"
    assert params["reduceOnly"] == "true"
    assert params["newClientOrderId"] == "abc123"


def _setup_pm():
    execution = PaperExecutionEngine(slippage_bps=0.0, fee_bps=0.0)
    risk = RiskManager(initial_equity=10_000)
    pm = PositionManager(execution, risk)
    return pm, risk


def test_open_position_registers_risk():
    pm, risk = _setup_pm()
    signal = make_signal()
    tps = [TakeProfitLeg(price=110, fraction=0.5, label="TP1"), TakeProfitLeg(price=120, fraction=0.5, label="TP2")]
    pos = pm.open_position(signal=signal, symbol="BTCUSDT", side=TradeSide.LONG, entry_price=100,
                            quantity=10, stop_loss=95, take_profits=tps, wave_label=WaveLabel.W3)
    assert pos.position_id in risk.open_risk
    assert pos.entry_price == 100


def test_stop_loss_hit_closes_full_position():
    pm, risk = _setup_pm()
    signal = make_signal()
    tps = [TakeProfitLeg(price=110, fraction=1.0, label="TP1")]
    pos = pm.open_position(signal=signal, symbol="BTCUSDT", side=TradeSide.LONG, entry_price=100,
                            quantity=10, stop_loss=95, take_profits=tps, wave_label=WaveLabel.W3)
    result = pm.on_price_update(pos.position_id, price=94, now_ms=1)
    assert result == "STOP_LOSS"
    assert pos.closed
    assert pos.realized_pnl < 0
    assert pos.position_id not in risk.open_risk


def test_partial_tp_fill_reduces_quantity_without_closing():
    pm, risk = _setup_pm()
    signal = make_signal()
    tps = [TakeProfitLeg(price=110, fraction=0.5, label="TP1"), TakeProfitLeg(price=120, fraction=0.5, label="TP2")]
    pos = pm.open_position(signal=signal, symbol="BTCUSDT", side=TradeSide.LONG, entry_price=100,
                            quantity=10, stop_loss=95, take_profits=tps, wave_label=WaveLabel.W3)
    result = pm.on_price_update(pos.position_id, price=111, now_ms=1)
    assert result == "TP_HIT:TP1"
    assert not pos.closed
    assert pos.quantity == pytest.approx(5.0)
    assert pos.realized_pnl > 0


def test_full_tp_sequence_closes_position():
    pm, risk = _setup_pm()
    signal = make_signal()
    tps = [TakeProfitLeg(price=110, fraction=0.5, label="TP1"), TakeProfitLeg(price=120, fraction=0.5, label="TP2")]
    pos = pm.open_position(signal=signal, symbol="BTCUSDT", side=TradeSide.LONG, entry_price=100,
                            quantity=10, stop_loss=95, take_profits=tps, wave_label=WaveLabel.W3)
    pm.on_price_update(pos.position_id, price=111, now_ms=1)
    pm.on_price_update(pos.position_id, price=121, now_ms=2)
    assert pos.closed
    assert pos.position_id not in risk.open_risk
    assert pos in pm.closed_positions


def test_trailing_stop_cannot_widen():
    pm, risk = _setup_pm()
    signal = make_signal()
    tps = [TakeProfitLeg(price=110, fraction=1.0, label="TP1")]
    pos = pm.open_position(signal=signal, symbol="BTCUSDT", side=TradeSide.LONG, entry_price=100,
                            quantity=10, stop_loss=95, take_profits=tps, wave_label=WaveLabel.W3)
    pm.update_trailing_stop(pos.position_id, 98)  # tightening, ok
    assert pos.stop_loss == 98
    with pytest.raises(StopWideningRejected):
        pm.update_trailing_stop(pos.position_id, 96)  # widening, rejected


def test_mae_mfe_tracked():
    pm, risk = _setup_pm()
    signal = make_signal()
    tps = [TakeProfitLeg(price=130, fraction=1.0, label="TP1")]
    pos = pm.open_position(signal=signal, symbol="BTCUSDT", side=TradeSide.LONG, entry_price=100,
                            quantity=10, stop_loss=80, take_profits=tps, wave_label=WaveLabel.W3)
    pm.on_price_update(pos.position_id, price=90, now_ms=1)  # adverse
    pm.on_price_update(pos.position_id, price=115, now_ms=2)  # favorable
    assert pos.mae == pytest.approx(10.0)
    assert pos.mfe == pytest.approx(15.0)
